"""Function builds, driven by kpack ``Image`` CRs (docs/BUILDING.md - Ownership).

The API does not run builds; it declares them. Each create/update emits the
function's git Secret, build ServiceAccount and ``Image`` alongside the KSVC's
other derived resources, and kpack does the rest: clone, detect, build with
Cloud Native Buildpacks, push to the internal registry.

They are owned resources of the workload, in its own namespace, so the KSVC's
ownerReference garbage-collects them. That co-location is also why a function
needs only ONE git Secret: kpack reads the workload's own ``{workload}-git``.

Declaring is not completing, so ``plan`` returns the deterministic tag the
build will push to. Callers deploy against that tag and read progress back
through :meth:`KpackBackend.status` - the reason a just-created function reports
``Building`` rather than ``Ready`` (docs/FUNCTIONS.md - Function Status Resolution).

This is the in-process implementation of :class:`common.build.BuildBackend`;
``Builder`` throughout this module means kpack's own ``Builder`` CR, never the
protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from cloudlet_apis.logging import get_logger

from api.services.builder.runtimes import RuntimeRegistry
from api.services.manifests import secrets as secret_svc
from common import kpack
from common.build import (
    BuildPlan,
    BuildRequest,
    BuildStatus,
    SiteBuild,
    cache_reference,
    image_reference,
)
from common.cluster import Cluster, ResourceKind
from common.config import BuildConfig, RegistryConfig
from common.errors import NotFoundError, ValidationError
from common.labels import LABEL_GROUP, LABEL_OFFERING, LABEL_WORKLOAD, OFFERING_FUNCTION
from common.names import object_name

logger = get_logger(__name__)


class KpackBackend:
    """Emits the kpack manifests for a function build and reads their state."""

    def __init__(self, build: BuildConfig, runtimes: RuntimeRegistry):
        """Initialize the backend.

        Args:
            build: Build settings (credentials, cache mode, resources, history).
            runtimes: Resolves a runtime to its kpack Builder and build environment.
        """
        self._build = build
        self._runtimes = runtimes
        # Both registries a build reads: this site's, and the kpack registry the
        # run image comes from. Empty entries are dropped, so an install with one
        # registry names one Secret.
        self._registry_secrets = [
            s for s in (build.registry_secret, build.kpack_registry_secret) if s
        ]

    @property
    def pull_secret(self) -> str | None:
        """The registry Secret a built function's KSVC pulls its image with.

        The same credential kpack pushes with - one image, one registry, one
        credential - so a function never needs registry details from the caller.
        None when unset, so the KSVC does not reference a Secret that the chart
        never created.
        """
        return self._build.registry_secret or None

    def image_ref(self, req: BuildRequest, registry: RegistryConfig) -> str:
        """The image reference a build pushes to (deterministic, no cluster call).

        Args:
            req: The build request.
            registry: The registry that build pushes to - one site's.

        Returns:
            The fully-qualified image reference.
        """
        return image_reference(registry.base, req)

    def cache_ref(self, req: BuildRequest, registry: RegistryConfig) -> str | None:
        """Where a build caches its layers, or None to leave it to kpack.

        A sibling of the image repository in the same registry, so the cache
        follows the image rather than being pulled across sites.

        Args:
            req: The build request.
            registry: The registry that build pushes to.

        Returns:
            The registry cache reference, or None when ``build.cache`` is
            ``inherit`` and the Image should carry no ``spec.cache``.
        """
        if self._build.cache != "registry":
            return None
        return cache_reference(registry.base, req)

    def _runtime_config(self, runtime: str, version: str | None = None) -> tuple[str, list[dict]]:
        """Resolve a runtime (and optional version) to ``(builder, build_env)``.

        The runtimes file is rendered by the chart with each entry's build
        environment already merged (shared env, dependency mirror, per-runtime
        overrides), so nothing is composed here beyond pinning the version.

        The version variable is **always** written when the runtime names one,
        including when the caller asked for nothing. Leaving it unset would hand
        the choice to the buildpack's own default, which moves when the
        buildpackage is upgraded - so an untouched function could silently
        rebuild on a different language version, and airgapped it could ask for a
        toolchain that was never mirrored. Precedence is caller, then an explicit
        ``buildEnv`` pin, then the platform default: a caller can only choose from
        the list the operator advertised, so an operator who wants no choice
        leaves ``versions`` empty and the request is rejected before here.

        Args:
            runtime: The requested runtime name.
            version: The requested language version, or None for the default.

        Returns:
            The Builder name and the build env list.

        Raises:
            ValidationError: If the runtime is unknown or names no Builder.
        """
        spec = self._runtimes.get(runtime)
        if spec is None:
            available = ", ".join(self._runtimes.names())
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )
        if not spec.builder:
            raise ValidationError(
                f"runtime '{runtime}' has no `builder`; the runtimes ConfigMap must map "
                "every runtime to a kpack Builder before functions can be built."
            )
        env = [dict(e) for e in spec.buildEnv]
        if spec.versionEnv:
            pinned = next((e.get("value") for e in env if e.get("name") == spec.versionEnv), None)
            chosen = version or pinned or spec.defaultVersion
            if chosen:
                env = [e for e in env if e.get("name") != spec.versionEnv]
                env.append({"name": spec.versionEnv, "value": chosen})
        return spec.builder, env

    def plan(
        self, req: BuildRequest, labels: dict[str, str], registries: Mapping[str, RegistryConfig]
    ) -> BuildPlan:
        """The build manifests for one function, split by replication scope.

        Pure - no cluster call - so the caller can apply them in the same pass as
        the KSVC's other derived resources and have them owner-stamped.

        The git Secret is replicated; the Image and ServiceAccount are per site.
        Each site pushes to its own registry, so the objects are identical but
        for the tag and the cache reference, and no two sites contend for one
        repository (docs/PER-SITE-REGISTRY.md).

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.
            registries: The registry each building site pushes to, keyed by site
                name. Its keys are the sites that build - the workload's
                targets. Passed in rather than resolved here: the caller holds
                the clusters, and each carries its own registry.

        Returns:
            The build plan; each site's manifests are in dependency order.

        Raises:
            ValidationError: If the runtime is unknown or maps to no Builder.
        """
        oname = object_name(req.name, req.group)
        builder, env = self._runtime_config(req.runtime, req.version)
        build_name = kpack.build_object_name(oname)
        git_secret = secret_svc.git_secret_name(oname)
        per_site: dict[str, SiteBuild] = {}
        for site, registry in registries.items():
            tag = self.image_ref(req, registry)
            per_site[site] = SiteBuild(
                tag=tag,
                manifests=[
                    kpack.build_service_account(
                        build_name, labels, git_secret, self._registry_secrets
                    ),
                    kpack.build_image(
                        build_name,
                        labels,
                        tag=tag,
                        builder=builder,
                        service_account=build_name,
                        git_url=req.git_url,
                        revision=req.build_revision,
                        sub_path=req.path,
                        env=env,
                        resources=self._build.resources,
                        cache_tag=self.cache_ref(req, registry),
                        success_history_limit=self._build.success_history_limit,
                        failed_history_limit=self._build.failed_history_limit,
                    ),
                ],
            )
        return BuildPlan(
            replicated=[
                secret_svc.build_git_secret(
                    git_secret, labels, req.git_token, req.git_url, self._build.git_username
                )
            ],
            per_site=per_site,
        )

    def trigger(self, cluster: Cluster, name: str, group: str) -> bool:
        """Ask kpack for one more build of the function's current inputs.

        Patches the annotation onto the latest ``Build``, which is where kpack
        looks for it (:data:`~common.kpack.BUILD_TRIGGER_ANNOTATION`) and the
        reason a rebuild leaves the ``Image`` untouched: the desired state stays
        a pure function of the function definition, so the next ordinary apply
        neither carries a nonce forward nor drops one and rebuilds again
        (docs/BUILDING.md - Convergence rules).

        Args:
            cluster: The cluster holding the Image (always the local site).
            name: The workload name.
            group: The owning group.

        Returns:
            True if a build was triggered; False when the Image has no build yet
            - it is about to make one, and there is nothing to annotate.

        Raises:
            Exception: If the Builds could not be listed or the patch failed.
                Unlike a status read, this one is the whole point of the call: a
                swallowed error is a rebuild that silently never happens.
        """
        image_name = kpack.build_object_name(object_name(name, group))
        builds = cluster.get(
            ResourceKind.KPACK_BUILD, label_selector=f"{kpack.IMAGE_LABEL}={image_name}"
        )
        latest = kpack.latest_build(builds)
        if latest is None:
            logger.info(
                "no build to trigger for Image '%s' on %s; it has one coming",
                image_name,
                cluster.site,
            )
            return False
        build_name = (latest.get("metadata") or {}).get("name")
        cluster.patch(
            ResourceKind.KPACK_BUILD,
            build_name,
            kpack.trigger_patch(datetime.now(UTC).isoformat(timespec="seconds")),
        )
        logger.info(
            "triggered a rebuild of Image '%s' on %s via build '%s'",
            image_name,
            cluster.site,
            build_name,
        )
        return True

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """Read a function's build state from one cluster.

        Args:
            cluster: The cluster to read (normally the local site).
            name: The workload name.
            group: The owning group.

        Returns:
            The build status, or None when the function has no Image on this
            cluster - which is the normal case for a site that has never built
            it, and must fall through to the KSVC status rather than read as a
            failure (docs/FUNCTIONS.md - Function Status Resolution).
        """
        image_name = kpack.build_object_name(object_name(name, group))
        try:
            image = cluster.get(ResourceKind.KPACK_IMAGE, image_name)
        except NotFoundError:
            return None
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning("could not read kpack Image '%s' on %s", image_name, cluster.site)
            return None
        state, latest, message = kpack.build_status(image)
        return BuildStatus(state=state, image=latest, message=message)

    def statuses(self, cluster: Cluster, group: str) -> dict[str, BuildStatus]:
        """Read every function build state a group has on one cluster, in one call.

        Keyed by the ``workload`` label - the object name ``{name}-{group}`` -
        because that is what the Image carries and what the caller already has
        from the KSVC it is annotating. The Image's own name (``fn-{workload}``)
        is an implementation detail of this module.

        Never an error: a listing that could not read kpack falls through to the
        KSVC statuses, exactly as :meth:`status` does for a single workload.

        Args:
            cluster: The cluster to read (normally the local site).
            group: The owning group.

        Returns:
            ``{object_name: BuildStatus}``; empty when the group has no builds or
            kpack could not be read.
        """
        selector = f"{LABEL_GROUP}={group},{LABEL_OFFERING}={OFFERING_FUNCTION}"
        try:
            images = cluster.get(ResourceKind.KPACK_IMAGE, label_selector=selector)
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning("could not list kpack Images for group '%s' on %s", group, cluster.site)
            return {}
        out: dict[str, BuildStatus] = {}
        for image in images:
            workload = ((image.get("metadata") or {}).get("labels") or {}).get(LABEL_WORKLOAD)
            if not workload:
                continue  # not one of ours to attribute to a workload
            state, latest, message = kpack.build_status(image)
            out[workload] = BuildStatus(state=state, image=latest, message=message)
        return out
