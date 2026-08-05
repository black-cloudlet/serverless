"""Function builds, driven by kpack ``Image`` CRs (docs/BUILDING.md - Ownership)."""

from __future__ import annotations

from datetime import UTC, datetime

from api.services.builder.runtimes import RuntimeRegistry
from api.services.manifests import secrets as secret_svc
from common import kpack
from common.build import (
    BuildPlan,
    BuildRequest,
    BuildStatus,
    cache_reference,
    image_reference,
)
from common.cluster import Cluster, ResourceKind
from common.config import CommonSettings
from common.errors import NotFoundError, ValidationError
from common.labels import LABEL_GROUP, LABEL_OFFERING, LABEL_WORKLOAD, OFFERING_FUNCTION
from common.logging import get_logger
from common.names import object_name

logger = get_logger(__name__)


class KpackBackend:
    """Emits the kpack manifests for a function build and reads their state."""

    def __init__(self, settings: CommonSettings, runtimes: RuntimeRegistry):
        """Initialize the backend."""
        self._registry = settings.registry
        self._build = settings.build
        self._runtimes = runtimes

    @property
    def pull_secret(self) -> str | None:
        """The registry Secret a built function's KSVC pulls its image with."""
        return self._build.registry_secret or None

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference a build pushes to (deterministic, no cluster call)."""
        return image_reference(self._registry.base, req)

    def cache_ref(self, req: BuildRequest) -> str | None:
        """Where this build caches its layers, or None to leave it to kpack."""
        if self._build.cache != "registry":
            return None
        return cache_reference(self._registry.base, req)

    def _runtime_config(self, runtime: str, version: str | None = None) -> tuple[str, list[dict]]:
        """Resolve a runtime (and optional version) to ``(builder, build_env)``.

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

    def plan(self, req: BuildRequest, labels: dict[str, str]) -> BuildPlan:
        """The build manifests for one function, split by replication scope.

        Pure - no cluster call - so the caller can apply them in the same pass as
        the KSVC's other derived resources and have them owner-stamped.

        Raises:
            ValidationError: If the runtime is unknown or maps to no Builder.
        """
        oname = object_name(req.name, req.group)
        builder, env = self._runtime_config(req.runtime, req.version)
        tag = self.image_ref(req)
        build_name = kpack.build_object_name(oname)
        git_secret = secret_svc.git_secret_name(oname)
        return BuildPlan(
            tag=tag,
            replicated=[
                secret_svc.build_git_secret(
                    git_secret, labels, req.git_token, req.git_url, self._build.git_username
                )
            ],
            local=[
                kpack.build_service_account(
                    build_name, labels, git_secret, self._build.registry_secret
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
                    cache_tag=self.cache_ref(req),
                    success_history_limit=self._build.success_history_limit,
                    failed_history_limit=self._build.failed_history_limit,
                ),
            ],
        )

    def trigger(self, cluster: Cluster, name: str, group: str) -> bool:
        """Ask kpack for one more build of the function's current inputs.

        Raises:
            Exception: If the Builds could not be listed or the patch failed.
                Not swallowed like a status read: that would be a rebuild that
                silently never happens.
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
        """Read a function's build state from one cluster."""
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

        Never an error: a listing that could not read kpack falls through to the
        KSVC statuses, exactly as :meth:`status` does for a single workload.
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
