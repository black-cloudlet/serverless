"""The control loop: watch local kpack Images, roll their digests out everywhere.

One pass is a full relist followed by a watch resumed from it, so the loop is
event-driven without depending on having seen every event - a dropped stream or
an expired ``resourceVersion`` costs one extra relist, not a stalled function.

Reads are **local only**. The `Image` for a function exists in exactly one
cluster, the one that built it (docs/BUILDING.md - Active/Active Behaviour), and
that is this one. Writes go to **every** site: the registry is shared, so a site
that only runs the workload pulls what this site pushed.

No leader election. Two replicas, or the two sites' controllers seeing the same
digest, apply the same desired state, and a server-side apply of identical
content is a no-op that produces no Knative revision.
"""

from __future__ import annotations

from common import kpack
from common.cluster import Cluster, ResourceKind, clusters_for, select_local
from common.config import CommonSettings
from common.errors import NotFoundError
from common.labels import (
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
    OFFERING_FUNCTION,
)
from common.logging import get_logger
from controller.digest import needs_image, with_image

logger = get_logger(__name__)

# Only this platform's function builds. A kpack install is shared infrastructure
# and may carry Images nothing here owns.
IMAGE_SELECTOR = f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE},{LABEL_OFFERING}={OFFERING_FUNCTION}"


class Reconciler:
    """Propagates ``Image.status.latestImage`` to the function's KSVC in every site."""

    def __init__(self, settings: CommonSettings):
        """Build the per-site clients and pick the one holding the Images.

        Args:
            settings: Shared settings (sites, local site, TLS material).

        Raises:
            ValidationError: If no sites are configured - the loop would have
                nothing to watch and nowhere to write.
        """
        self._clusters = clusters_for(settings)
        self._local = select_local(self._clusters, settings.local_site)

    @property
    def local(self) -> Cluster:
        """The cluster whose Images this loop follows."""
        return self._local

    def close(self) -> None:
        """Release every site's cluster client at shutdown."""
        for cluster in self._clusters.values():
            cluster.close()

    def resync(self) -> str | None:
        """Reconcile every Image once, and return where a watch should resume.

        Returns:
            The listing's resourceVersion, or None to have the watch start from
            now (which is safe: the relist just reconciled everything).
        """
        images, version = self._local.list_resources(
            ResourceKind.KPACK_IMAGE, label_selector=IMAGE_SELECTOR
        )
        for image in images:
            self.reconcile(image)
        logger.info("resynced %d image(s) from %s", len(images), self._local.site)
        return version

    def follow(self, timeout_seconds: int) -> None:
        """Reconcile each Image change until the server closes the stream.

        Args:
            timeout_seconds: How long to hold the watch open.
        """
        version = self.resync()
        for _event, image in self._local.watch(
            ResourceKind.KPACK_IMAGE,
            resource_version=version,
            label_selector=IMAGE_SELECTOR,
            timeout_seconds=timeout_seconds,
        ):
            self.reconcile(image)

    def reconcile(self, image: dict) -> None:
        """Roll one Image's last successful digest onto its function, everywhere.

        ``latestImage`` is the last *successful* build, so a function whose newest
        build failed keeps serving the one before it - the right outcome, and the
        reason the Image's ready state is not consulted here.

        Args:
            image: The kpack Image object.
        """
        workload = ((image.get("metadata") or {}).get("labels") or {}).get(LABEL_WORKLOAD)
        _state, digest, _message = kpack.build_status(image)
        if not workload or not digest:
            return  # never built, or not attributable to a workload
        for cluster in self._clusters.values():
            self._roll_out(cluster, workload, digest)

    def _roll_out(self, cluster: Cluster, workload: str, digest: str) -> bool:
        """Apply the digest to one site's KSVC, if that is what it needs.

        Per-site failures are logged, never raised: one unreachable site must not
        stop the others, and the next resync retries anyway.

        Args:
            cluster: The site to write to.
            workload: The object name (``{name}-{group}``).
            digest: The image reference to run.

        Returns:
            True if the KSVC was applied.
        """
        try:
            ksvc = cluster.get(ResourceKind.KNATIVE_SERVICE, workload)
        except NotFoundError:
            return False  # not deployed to this site
        except Exception:  # noqa: BLE001 - one site's failure is not the loop's
            logger.exception("could not read '%s' in %s", workload, cluster.site)
            return False
        if not needs_image(ksvc, digest):
            return False
        try:
            cluster.apply(with_image(ksvc, digest))
        except Exception:  # noqa: BLE001 - retried by the next resync
            logger.exception("could not roll '%s' onto '%s' in %s", digest, workload, cluster.site)
            return False
        logger.info("rolled '%s' onto '%s' in %s", digest, workload, cluster.site)
        return True
