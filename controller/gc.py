"""Tag GC: prune the per-build registry tags a function accumulates here.

kpack pushes every successful build twice: the branch tag moves to the new
digest, and a unique ``b{build}.{date}.{time}`` tag is added beside it. The
branch tag overwrites; the build tags accumulate, one per build, for the life
of the function - and CVE rebuilds and ``POST .../build`` create builds without
any user action, so they grow even for functions nobody touches. Nothing else
reclaims them short of deleting the function
(docs/BUILDING.md - Registry tag GC).

Local by design, like the reconciler this rides in: a site builds what it runs
and pushes to its own registry, so each controller prunes exactly the registry
its site filled, and no cross-site call exists. And *reconciled*, unlike the
API's fire-once cleanup on delete: garbage is re-derived from live state every
sweep, so a crash or an unreachable registry leaks nothing permanently - the
next sweep collects it.

What survives a sweep (:func:`garbage`), and why deleting the rest is safe:

- the function's **current branch tag** (the tag half of ``Image.spec.tag``) -
  a create deploys at it, and a switchover site rebuilds into it;
- every tag still pointing at the **digest of** ``status.latestImage`` -
  deleting the last tag on a manifest lets the registry collect the manifest,
  and the KSVC pinned to that digest could no longer pull on a node change;
- the **newest** ``gc_keep_builds`` build tags beyond those, so recent builds
  stay addressable, mirroring the Build history kpack itself keeps;
- any tag the listing reports **without a digest** - it cannot be proven safe.

An older *revision* can pin a digest outside that set; Quay's time machine is
the accepted backstop for one that re-pulls after its tags are pruned
(docs/BUILDING.md - Registry tag GC).
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from cloudlet_apis.logging import get_logger

from common import kpack
from common.labels import LABEL_WORKLOAD
from common.registry import RegistryClient, TagInfo, repository_path
from controller.config import ControllerSettings

logger = get_logger(__name__)


def tag_of(reference: str) -> str | None:
    """The tag half of an image reference, or None when it carries none.

    The counterpart of :func:`common.names.repository_of`, splitting the same
    grammar the same way: the digest is cut first, and a ``:`` counts as a tag
    separator only past the last ``/`` - a registry host may carry a port.

    Args:
        reference: An image reference (already validated).

    Returns:
        The tag, or None for a bare or digest-only reference.
    """
    untagged = reference.split("@", 1)[0]
    tail = untagged.rpartition("/")[2]
    if ":" not in tail:
        return None
    return tail.split(":", 1)[1]


def digest_of(reference: str | None) -> str | None:
    """The digest an image reference pins, or None when it names none.

    Args:
        reference: An image reference, or None.

    Returns:
        The ``algorithm:hex`` half - the form Quay's tag listing reports as
        ``manifest_digest``, so the two compare directly.
    """
    if reference and "@" in reference:
        return reference.rsplit("@", 1)[1]
    return None


def garbage(
    tags: Iterable[TagInfo],
    *,
    protected_tags: set[str],
    protected_digests: set[str],
    keep: int,
) -> list[TagInfo]:
    """The tags nothing needs anymore - the pure half of the sweep.

    Protected names are set aside first, so the newest-``keep`` window is spent
    entirely on build tags: the branch tag is re-pushed by every build and
    would otherwise occupy the newest slot on every sweep, silently shrinking
    the window by one.

    Args:
        tags: The repository's active tags, in any order.
        protected_tags: Tag names that survive regardless of age or digest.
        protected_digests: Manifest digests whose every tag survives.
        keep: How many of the newest unprotected tags survive as well.

    Returns:
        The tags to delete, newest first. A tag with no digest is never among
        them - it cannot be proven safe, and the safe direction is to keep it.
    """
    newest_first = sorted(tags, key=lambda t: (t.start_ts, t.name), reverse=True)
    candidates = [t for t in newest_first if t.name not in protected_tags]
    return [t for t in candidates[max(keep, 0) :] if t.digest and t.digest not in protected_digests]


class TagGC:
    """The periodic sweep: every function's repository, against live state.

    Constructed with the *resolved* site name - which cluster this controller
    actually watches - so the registry it prunes is the one that site's builds
    fill. Enabled only when configuration says so AND that registry has an API
    token; either way it says which at startup, once, so an operator reading
    the log knows whether tags are being reclaimed without deducing it from
    silence.
    """

    def __init__(self, settings: ControllerSettings, site: str):
        """Resolve the site's registry and decide, audibly, whether to run.

        Args:
            settings: Controller settings (registry, pacing, GC knobs).
            site: The resolved local site name.
        """
        self._site = site
        self._registry = settings.registry_for(site)
        self._keep = settings.gc_keep_builds
        self._interval = settings.gc_interval_seconds
        self._enabled = bool(settings.gc_enabled and self._registry.api_token)
        # Monotonic deadline; zero means the first resync sweeps immediately,
        # so a restarted controller shows its GC working within one pass.
        self._next_sweep = 0.0
        if not settings.gc_enabled:
            logger.info("tag GC off: disabled by configuration")
        elif not self._registry.api_token:
            logger.info(
                "tag GC off: no registry API token for site '%s' (%s); "
                "old build tags will accumulate until their functions are deleted",
                site,
                self._registry.host,
            )
        else:
            logger.info(
                "tag GC on: pruning old build tags in %s every %ds, "
                "keeping the newest %d build tag(s) per function",
                self._registry.host,
                self._interval,
                self._keep,
            )

    def maybe_sweep(self, images: list[dict]) -> None:
        """Sweep when due, and never raise into the reconcile loop.

        Called with each resync's listing, so being due costs no second LIST.
        The next deadline is set before the attempt, not after success: a
        registry that keeps failing retries at the next *due* resync, and the
        loop's actual job - rolling out digests - never queues behind it.

        Args:
            images: The kpack Images the resync just listed.
        """
        if not self._enabled:
            return
        now = time.monotonic()
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._interval
        try:
            self.sweep(images)
        except Exception:  # noqa: BLE001 - a failed sweep is logged, not the loop's end
            logger.exception(
                "tag GC: sweep failed in '%s'; retrying in ~%ds", self._site, self._interval
            )

    def sweep(self, images: Iterable[dict]) -> None:
        """Prune every function's repository, one connection for the lot.

        Args:
            images: The kpack Images to derive repositories and live state from.
        """
        started = time.monotonic()
        swept = pruned = 0
        with RegistryClient(self._registry) as client:
            for image in images:
                outcome = self._sweep_one(client, image)
                if outcome is None:
                    continue
                swept += 1
                pruned += outcome
        logger.info(
            "tag GC: swept %d function repositories in '%s', pruned %d tag(s), in %.1fs",
            swept,
            self._site,
            pruned,
            time.monotonic() - started,
        )

    def _sweep_one(self, client: RegistryClient, image: dict) -> int | None:
        """Prune one function's image repository.

        The cache repository is never touched: it reuses one ``latest`` tag and
        does not accumulate (docs/BUILDING.md - Open Questions).

        Args:
            client: The open registry connection.
            image: The kpack Image, as the resync listed it.

        Returns:
            How many tags were deleted, or None when the Image was skipped.
        """
        metadata = image.get("metadata") or {}
        workload = (metadata.get("labels") or {}).get(LABEL_WORKLOAD) or metadata.get("name", "?")
        reference = (image.get("spec") or {}).get("tag") or ""
        if not reference:
            return None  # nothing was ever pushed for an Image naming no tag
        repo = repository_path(self._registry, reference)
        if repo is None:
            logger.warning(
                "tag GC: skipping '%s': its tag '%s' is not on %s",
                workload,
                reference,
                self._registry.host,
            )
            return None
        _state, latest, _message = kpack.build_status(image)
        protected_tags = {t for t in (tag_of(reference),) if t}
        protected_digests = {d for d in (digest_of(latest),) if d}
        tags = client.list_tags(repo)
        doomed = garbage(
            tags,
            protected_tags=protected_tags,
            protected_digests=protected_digests,
            keep=self._keep,
        )
        if not doomed:
            return 0
        # Each delete logs its own tag name in RegistryClient; this line is the
        # per-function verdict an operator greps for.
        deleted = sum(1 for tag in doomed if client.delete_tag(repo, tag.name))
        logger.info(
            "tag GC: '%s': pruned %d of %d tag(s) in '%s' (kept the branch tag, "
            "the digest still serving, and the %d newest build tag(s))",
            workload,
            deleted,
            len(tags),
            repo,
            self._keep,
        )
        return deleted
