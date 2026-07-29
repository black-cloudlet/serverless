"""Function builds, driven by kpack ``Image`` CRs (docs/BUILD-PIPELINE.md §8).

The API does not run builds; it declares them. Each create/update emits the
function's git Secret, build ServiceAccount and ``Image`` alongside the KSVC's
other derived resources, and kpack does the rest: clone, detect, build with
Cloud Native Buildpacks, push to the internal registry.

They are ordinary owned resources of the workload, in the workload's own
namespace, so the KSVC's ownerReference garbage-collects them - deleting a
function cannot leave an Image behind that rebuilds it forever. That
co-location is also why a function needs only ONE git Secret: kpack reads the
workload's own ``{workload}-git``.

Declaring is not completing, so ``manifests`` returns the deterministic tag the
build will push to. Callers deploy against that tag and read progress back
through :meth:`KpackBuilder.status` - the reason a just-created function reports
``Building`` rather than ``Ready`` (§10).
"""

from __future__ import annotations

from api.services import kpack
from api.services import secrets as secret_svc
from api.services.runtimes import RuntimeRegistry
from common.cluster import Cluster, ResourceKind
from common.config import CommonSettings
from common.contract import BuildRequest, BuildStatus, image_reference
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger

logger = get_logger(__name__)


class KpackBuilder:
    """Emits the kpack manifests for a function build and reads their state."""

    def __init__(self, settings: CommonSettings, runtimes: RuntimeRegistry):
        """Initialize the builder.

        Args:
            settings: Shared settings (registry, build credentials).
            runtimes: Resolves a runtime to its Builder and build environment.
        """
        self._registry = settings.registry
        self._build = settings.build
        self._runtimes = runtimes

    @property
    def pull_secret(self) -> str:
        """The registry Secret a built function's KSVC pulls its image with.

        The same credential kpack pushes with - one image, one registry, one
        credential - so a function never needs registry details from the caller.
        """
        return self._build.registry_secret

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference a build pushes to (deterministic, no cluster call).

        Args:
            req: The build request.

        Returns:
            The fully-qualified image reference.
        """
        return image_reference(self._registry.base, req)

    def _runtime_config(self, runtime: str) -> tuple[str, list[dict], dict]:
        """Resolve a runtime to ``(builder, build_env, resources)``.

        The runtimes file is rendered by the chart with each entry's build
        environment already merged (shared env, dependency mirror, per-runtime
        overrides), so nothing is composed here beyond adding the version.

        Args:
            runtime: The requested runtime name.

        Returns:
            The Builder name, the build env list, and the build pod resources.

        Raises:
            ValidationError: If the runtime is unknown or names no Builder.
        """
        spec = self._runtimes.get(runtime)
        if spec is None:
            available = ", ".join(self._runtimes.names())
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )
        extra = spec.model_dump()
        builder = extra.get("builder")
        if not builder:
            raise ValidationError(
                f"runtime '{runtime}' has no `builder`; the runtimes ConfigMap must map "
                "every runtime to a kpack Builder before functions can be built."
            )
        env = [dict(e) for e in (extra.get("buildEnv") or [])]
        # Pin the language version the same way the chart documents it: the
        # runtime names the env var, so a version bump is a ConfigMap edit.
        version_env, version = extra.get("versionEnv"), extra.get("defaultVersion")
        if version_env and version and not any(e.get("name") == version_env for e in env):
            env.append({"name": version_env, "value": str(version)})
        resources = extra.get("buildResources") or self._build.resources
        return builder, env, dict(resources or {})

    def manifests(self, req: BuildRequest, labels: dict[str, str]) -> tuple[str, list[dict]]:
        """The build manifests for one function, and the tag they push to.

        Pure - no cluster call - so the caller can apply them in the same pass as
        the KSVC's other derived resources and have them owner-stamped.

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.

        Returns:
            ``(tag, manifests)``: the git Secret, the build ServiceAccount and
            the Image, in dependency order.

        Raises:
            ValidationError: If the runtime is unknown or maps to no Builder.
        """
        oname = f"{req.name}-{req.group}"
        builder, env, resources = self._runtime_config(req.runtime)
        tag = self.image_ref(req)
        object_name = kpack.build_object_name(oname)
        git_secret = secret_svc.git_secret_name(oname)
        return tag, [
            secret_svc.build_git_secret(
                git_secret, labels, req.git_token, req.git_url, self._build.git_username
            ),
            kpack.build_service_account(
                object_name, labels, git_secret, self._build.registry_secret
            ),
            kpack.build_image(
                object_name,
                labels,
                tag=tag,
                builder=builder,
                service_account=object_name,
                git_url=req.git_url,
                revision=req.build_revision,
                env=env,
                resources=resources,
            ),
        ]

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
            failure (§10).
        """
        object_name = kpack.build_object_name(f"{name}-{group}")
        try:
            image = cluster.get(ResourceKind.KPACK_IMAGE, object_name)
        except NotFoundError:
            return None
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning("could not read kpack Image '%s' on %s", object_name, cluster.site)
            return None
        state, latest, message = kpack.build_status(image)
        return BuildStatus(state=state, image=latest, message=message)
