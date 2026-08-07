"""The control loop: watch local kpack Images, roll their digests out everywhere.

Reads are local - a function's Image exists only in the cluster that built it -
and writes go to every site, which share the registry. One pass relists and then
watches from that point, so nothing is lost when a stream drops. No leader
election (docs/BUILDING.md - Digest propagation).

The same pass prunes the Images a switchover stranded in the other sites
(docs/BUILDING.md - Pruning stranded Images).
"""

from __future__ import annotations

from cloudlet_apis.logging import get_logger

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
from controller.digest import needs_image, with_image

logger = get_logger(__name__)

# A kpack install is shared; it may carry Images that are not this platform's.
IMAGE_SELECTOR = f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE},{LABEL_OFFERING}={OFFERING_FUNCTION}"


class Reconciler:
    """Propagates ``Image.status.latestImage`` to the function's KSVC in every site."""

    def __init__(self, settings: CommonSettings, *, prune_orphans: bool = True):
        """Build the per-site clients and pick the one holding the Images.

        Args:
            settings: Shared settings (sites, local site, TLS material).
            prune_orphans: Whether each pass also deletes Images this site has
                superseded in the others.

        Raises:
            ValidationError: If no sites are configured.
        """
        self._clusters = clusters_for(settings)
        self._local = select_local(self._clusters, settings.local_site)
        self._prune_orphans = prune_orphans

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
            The listing's resourceVersion, or None to watch from now - safe,
            since the relist just reconciled everything.
        """
        images, version = self._local.list_resources(
            ResourceKind.KPACK_IMAGE, label_selector=IMAGE_SELECTOR
        )
        for image in images:
            self.reconcile(image)
        logger.info("resynced %d image(s) from %s", len(images), self._local.site)
        if self._prune_orphans:
            self.prune(images)
        return version

    def prune(self, mine: list[dict]) -> int:
        """Delete Images in the other sites that this site has since superseded.

        The newer Image wins, so exactly one site prunes and two can never
        delete each other's. Deleting outward rather than inward is deliberate:
        the stranded site is the one that may be down. A site that cannot be
        listed stops the pass - deciding what is stranded from a partial view is
        how everything gets deleted (docs/BUILDING.md - Pruning stranded Images).

        Args:
            mine: This site's Images, already listed by :meth:`resync`.

        Returns:
            How many Images were deleted.
        """
        local = _by_name(mine)
        if not local:
            return 0
        others = {}
        for site, cluster in self._clusters.items():
            if cluster is self._local:
                continue
            try:
                images, _version = cluster.list_resources(
                    ResourceKind.KPACK_IMAGE, label_selector=IMAGE_SELECTOR
                )
            except Exception:  # noqa: BLE001 - an unread site is not an empty one
                logger.warning("could not list images in %s; skipping the prune", site)
                return 0
            others[site] = _by_name(images)

        pruned = 0
        for site, theirs in others.items():
            for name, stranded in theirs.items():
                if not _supersedes(local.get(name), stranded):
                    continue
                try:
                    self._clusters[site].delete(ResourceKind.KPACK_IMAGE, name)
                except NotFoundError:
                    continue
                except Exception:  # noqa: BLE001 - retried by the next pass
                    logger.exception("could not prune Image '%s' in %s", name, site)
                    continue
                pruned += 1
                logger.info("pruned Image '%s' stranded in %s", name, site)
        return pruned

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

        ``latestImage`` is the last *successful* build, so the ready state is
        not consulted: a failed newest build leaves the previous digest serving.

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

        Per-site failures are logged, not raised; the next resync retries.

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


def _by_name(images: list[dict]) -> dict[str, dict]:
    """Index Images by object name, skipping any that has none."""
    named = {}
    for image in images:
        name = (image.get("metadata") or {}).get("name")
        if name:
            named[name] = image
    return named


def _created(image: dict | None) -> str | None:
    """An Image's creationTimestamp, or None when it is missing or unusable."""
    if image is None:
        return None
    stamp = (image.get("metadata") or {}).get("creationTimestamp")
    return stamp if isinstance(stamp, str) and stamp else None


def _supersedes(mine: dict | None, theirs: dict) -> bool:
    """Whether ``mine`` is the newer of two Images for the same function.

    RFC3339 timestamps from two API servers, compared as strings because the
    format sorts. A tie, or either one unreadable, means no - the pass would
    otherwise turn a few seconds of clock skew into a deleted live build.
    """
    ours, other = _created(mine), _created(theirs)
    return bool(ours and other and ours > other)
