"""The control loop: watch local kpack Images, roll their digests onto the local KSVC.

Both ends are local: a region builds what it runs and pushes to its own registry
(docs/BUILDING.md - Active/Active Behaviour), so the Image is here because this
region built it, and the digest it produced is only pullable here. Nothing in
this loop reads or writes a peer cluster.

One pass relists and then watches from that point, so a dropped stream costs a
relist and no change is missed. There is no leader election
(docs/BUILD-CONTROLLER.md - Digest propagation).
"""

from __future__ import annotations

from collections.abc import Callable

from cloudlet_apis.logging import get_logger
from kubernetes.client.exceptions import ApiException

from build_controller.digest import needs_image, with_image
from build_controller.gc import TagGC
from common import kpack
from common.cluster import Cluster, NamespacedCluster, ResourceKind, clusters_for, select_local
from common.config import CommonSettings
from common.errors import NotFoundError
from common.labels import (
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
    OFFERING_FUNCTION,
)

logger = get_logger(__name__)

# A kpack install is shared; it may carry Images that are not this platform's.
IMAGE_SELECTOR = f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE},{LABEL_OFFERING}={OFFERING_FUNCTION}"


class Reconciler:
    """Propagates ``Image.status.latestImage`` to the function's KSVC in this region."""

    def __init__(
        self,
        settings: CommonSettings,
        gc_factory: Callable[[str], TagGC] | None = None,
    ):
        """Build the client for the region this controller sits in.

        Args:
            settings: Shared settings (regions, local region, TLS material).
            gc_factory: Builds the tag GC from the *resolved* region name, the
                region actually watched, which ``local_region`` may name only as
                a cluster or not at all. None runs the loop without GC.

        Raises:
            ValidationError: If no regions are configured.
        """
        # Every region is constructed only to pick this one out; the rest are
        # dropped unconnected, since nothing here touches a peer.
        self._local = select_local(clusters_for(settings), settings.local_region)
        # No namespace is bound: an Image lives in its group's namespace, so the
        # watch spans all of them and each roll-out uses the Image's own.
        self._gc = gc_factory(self._local.region) if gc_factory else None

    @property
    def local(self) -> Cluster:
        """The cluster whose Images this loop follows, and whose KSVCs it writes."""
        return self._local

    def close(self) -> None:
        """Release the cluster client at shutdown."""
        self._local.close()

    def resync(self) -> str | None:
        """Reconcile every Image once, and return where a watch should resume.

        Returns:
            The listing's resourceVersion, or None to watch from now, the relist
            having just reconciled everything.
        """
        images, version = self._local.list_resources(
            ResourceKind.KPACK_IMAGE, label_selector=IMAGE_SELECTOR, namespace=None
        )
        for image in images:
            self.reconcile(image)
        logger.info("resynced %d image(s) in %s", len(images), self._local.region)
        if self._gc is not None:
            # After the rollouts, on the same listing: the GC judges tags
            # against each Image's latest state, as just fetched. Paced
            # internally (docs/BUILD-CONTROLLER.md - Registry tag GC).
            self._gc.maybe_sweep(images)
        return version

    def follow(self, timeout_seconds: int) -> None:
        """Reconcile each Image change until the server closes the stream.

        Args:
            timeout_seconds: How long to hold the watch open.
        """
        version = self.resync()
        try:
            for _event, image in self._local.watch(
                ResourceKind.KPACK_IMAGE,
                resource_version=version,
                label_selector=IMAGE_SELECTOR,
                timeout_seconds=timeout_seconds,
                namespace=None,
            ):
                self.reconcile(image)
        except ApiException as exc:
            # 410 Gone is the server compacting history out from under the
            # watch; the next pass's relist resumes from a fresh listing.
            # Anything else takes the loop's error backoff.
            if exc.status != 410:
                raise
            logger.info("watch expired (resourceVersion too old); resyncing")

    def reconcile(self, image: dict) -> bool:
        """Roll one Image's last successful digest onto its function here.

        ``latestImage`` is the last *successful* build, so the ready state is
        not consulted: a failed newest build leaves the previous digest serving.

        Args:
            image: The kpack Image object.

        Returns:
            True if the KSVC was applied.
        """
        meta = image.get("metadata") or {}
        workload = (meta.get("labels") or {}).get(LABEL_WORKLOAD)
        namespace = meta.get("namespace")
        _state, digest, _message = kpack.build_status(image)
        if not workload or not digest or not namespace:
            return False  # never built, or not attributable to a workload
        return self._roll_out(workload, digest, namespace)

    def _roll_out(self, workload: str, digest: str, namespace: str) -> bool:
        """Apply the digest to the KSVC beside the Image, if that is what it needs.

        Failures are logged, not raised; the next resync retries.

        Args:
            workload: The workload's object name.
            digest: The image reference to run.
            namespace: The Image's own namespace, taken from the object, so the
                KSVC written is the one the Image was built for.

        Returns:
            True if the KSVC was applied.
        """
        cluster = NamespacedCluster(self._local, namespace)
        try:
            ksvc = cluster.get(ResourceKind.KNATIVE_SERVICE, workload)
        except NotFoundError:
            # An Image outliving its KSVC: the delete cascade has not collected
            # it yet.
            return False
        except Exception:  # noqa: BLE001 - one bad read is not the loop's end
            logger.exception("could not read '%s' in %s", workload, cluster.region)
            return False
        if not needs_image(ksvc, digest):
            return False
        try:
            cluster.apply(with_image(ksvc, digest))
        except Exception:  # noqa: BLE001 - retried by the next resync
            logger.exception(
                "could not roll '%s' onto '%s' in %s", digest, workload, cluster.region
            )
            return False
        logger.info("rolled '%s' onto '%s' in %s", digest, workload, cluster.region)
        return True
