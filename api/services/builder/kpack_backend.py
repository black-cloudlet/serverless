"""Function builds, driven by kpack ``Image`` CRs (docs/BUILDING.md - Ownership).

The API does not run builds; it declares them. Each create/update emits the
function's git Secret, build ServiceAccount and ``Image`` alongside the KSVC's
other derived resources, and kpack does the rest: clone, detect, build with
Cloud Native Buildpacks, push to the internal registry.

They are owned resources of the workload, in its own namespace, so the KSVC's
ownerReference garbage-collects them. That co-location is what makes ONE git
Secret enough: kpack reads the workload's own ``{workload}-git`` from the
ServiceAccount named on the ``Image``, in the ``Image``'s own namespace.

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
    RegionBuild,
    cache_reference,
    image_reference,
)
from common.cluster import NamespacedCluster, ResourceKind
from common.config import BuildConfig, RegistryConfig
from common.errors import NotFoundError, ValidationError
from common.labels import LABEL_GROUP, LABEL_OFFERING, LABEL_WORKLOAD, OFFERING_FUNCTION

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
        # Both registries a build reads: this region's, and the kpack registry the
        # run image comes from. Empty entries are dropped, so an install with one
        # registry names one Secret.
        self._registry_secrets = [
            s for s in (build.registry_secret, build.kpack_registry_secret) if s
        ]

    @property
    def pull_secret(self) -> str | None:
        """The registry Secret a built function's KSVC pulls its image with.

        The same credential kpack pushes with: one image, one registry, one
        credential, so a function never carries registry details from the caller.
        None when the setting is empty, so the KSVC never references a Secret the
        chart did not create (docs/BUILDING.md - Two registries, three credentials).
        """
        return self._build.registry_secret or None

    def image_ref(self, req: BuildRequest, registry: RegistryConfig) -> str:
        """The image reference a build pushes to (deterministic, no cluster call).

        Args:
            req: The build request.
            registry: The registry that build pushes to - one region's.

        Returns:
            The fully-qualified image reference.
        """
        return image_reference(registry.base, req)

    def cache_ref(self, req: BuildRequest, registry: RegistryConfig) -> str | None:
        """Where a build caches its layers, or None to leave it to kpack.

        A sibling repository of the image in the same registry the build pushes to
        (docs/BUILDING.md - Build cache).

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
        including when the caller asked for nothing, so the buildpack's own moving
        default never decides it. Precedence is the caller's version, then an
        explicit ``buildEnv`` pin, then the runtime's ``defaultVersion``; the
        entry for that variable is replaced, not appended to
        (docs/BUILDING.md - Axis 2 - runtime version). A version outside the
        runtime's advertised ``versions`` is rejected before this is reached.

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

        The git Secret is replicated; the Image and ServiceAccount are per region.
        Each region pushes to its own registry, so those objects are identical but
        for the tag and the cache reference (docs/BUILDING.md - Registry layout).

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.
            registries: The registry each building region pushes to, keyed by region
                name. Its keys are the regions that build - the workload's
                targets - and each is resolved by the caller from that region's
                cluster.

        Returns:
            The build plan; each region's manifests are in dependency order.

        Raises:
            ValidationError: If the runtime is unknown or maps to no Builder.
        """
        builder, env = self._runtime_config(req.runtime, req.version)
        image_name = kpack.build_image_name(req.name)
        sa_name = kpack.build_service_account_name(req.name)
        git_secret = secret_svc.git_secret_name(req.name)
        per_region: dict[str, RegionBuild] = {}
        for region, registry in registries.items():
            tag = self.image_ref(req, registry)
            per_region[region] = RegionBuild(
                tag=tag,
                manifests=[
                    kpack.build_service_account(
                        sa_name, labels, git_secret, self._registry_secrets
                    ),
                    kpack.build_image(
                        image_name,
                        labels,
                        tag=tag,
                        builder=builder,
                        service_account=sa_name,
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
            per_region=per_region,
        )

    def trigger(self, cluster: NamespacedCluster, name: str, group: str) -> bool:
        """Ask kpack for one more build of the function's current inputs.

        Patches :data:`~common.kpack.BUILD_TRIGGER_ANNOTATION` onto the latest
        ``Build``, which is where kpack looks for it. The ``Image`` is left
        untouched, so the desired state stays a pure function of the function
        definition and the next ordinary apply neither carries a nonce forward nor
        drops one and rebuilds again (docs/BUILDING.md - Convergence rules).

        Args:
            cluster: The cluster holding the Image (always the local region).
            name: The workload name.
            group: The owning group.

        Returns:
            True if a build was triggered; False when the Image has no build yet
            - it is about to make one, and there is nothing to annotate.

        Raises:
            Exception: If the Builds could not be listed or the patch failed.
                Propagated, not swallowed the way a status read's error is.
        """
        image_name = kpack.build_image_name(name)
        builds = cluster.get(
            ResourceKind.KPACK_BUILD, label_selector=f"{kpack.IMAGE_LABEL}={image_name}"
        )
        latest = kpack.latest_build(builds)
        if latest is None:
            logger.info(
                "no build to trigger for Image '%s' on %s; it has one coming",
                image_name,
                cluster.region,
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
            cluster.region,
            build_name,
        )
        return True

    def status(self, cluster: NamespacedCluster, name: str, group: str) -> BuildStatus | None:
        """Read a function's build state from one cluster.

        Args:
            cluster: The cluster to read (normally the local region).
            name: The workload name.
            group: The owning group.

        Returns:
            The build status, or None when the function has no Image on this
            cluster - the normal case for a region that has never built it, and
            the signal for the caller to fall through to the KSVC status
            (docs/FUNCTIONS.md - Function Status Resolution). Also None when the
            Image could not be read at all.
        """
        image_name = kpack.build_image_name(name)
        try:
            image = cluster.get(ResourceKind.KPACK_IMAGE, image_name)
        except NotFoundError:
            return None
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning("could not read kpack Image '%s' on %s", image_name, cluster.region)
            return None
        state, latest, message = kpack.build_status(image)
        return BuildStatus(state=state, image=latest, message=message)

    def statuses(self, cluster: NamespacedCluster, group: str) -> dict[str, BuildStatus]:
        """Read every function build state a group has on one cluster, in one call.

        One label-selected read per call, selecting on group and offering. Results are
        keyed by the ``workload`` label - the workload's own name, which is what the
        caller already holds from the KSVC it is annotating. The Image's name is the
        same string (:func:`common.kpack.build_image_name`), but the label is the
        selection contract, so it is what this read stands on.

        Never raises: a listing that could not read kpack returns empty and falls
        through to the KSVC statuses, as :meth:`status` does for a single workload
        (docs/FUNCTIONS.md - Function Status Resolution).

        Args:
            cluster: The cluster to read (normally the local region).
            group: The owning group.

        Returns:
            ``{workload: BuildStatus}``; empty when the group has no builds or
            kpack could not be read.
        """
        selector = f"{LABEL_GROUP}={group},{LABEL_OFFERING}={OFFERING_FUNCTION}"
        try:
            images = cluster.get(ResourceKind.KPACK_IMAGE, label_selector=selector)
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning(
                "could not list kpack Images for group '%s' on %s", group, cluster.region
            )
            return {}
        out: dict[str, BuildStatus] = {}
        for image in images:
            workload = ((image.get("metadata") or {}).get("labels") or {}).get(LABEL_WORKLOAD)
            if not workload:
                continue  # not one of ours to attribute to a workload
            state, latest, message = kpack.build_status(image)
            out[workload] = BuildStatus(state=state, image=latest, message=message)
        return out
