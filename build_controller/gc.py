"""Tag GC: prune the per-build registry tags a function accumulates here.

kpack pushes every successful build twice: the revision tag moves to the new
digest, and a unique ``b{build}.{date}.{time}`` tag is added beside it. The
revision tag overwrites; the build tags accumulate, one per build, for the life
of the function - and CVE rebuilds and ``POST .../build`` create builds without
any user action, so they grow even for functions nobody touches. Nothing else
reclaims them short of deleting the function
(docs/BUILD-CONTROLLER.md - Registry tag GC).

Local, like the reconciler this rides in: a region builds what it runs and
pushes to its own registry, so each controller prunes exactly the registry its
region filled, and makes no cross-region call. The premise is checked - a
controller that finds another region on its registry host refuses to sweep. The
sweep is reconciling: garbage is re-derived from live state every time, so an
interrupted or failed sweep leaks nothing permanently and the next one collects
it.

What survives a sweep (:func:`garbage`):

- the function's **current revision tag** (the tag half of ``Image.spec.tag``);
- every tag still pointing at the **digest of** ``status.latestImage``, since
  deleting the last tag on a manifest lets the registry collect the manifest
  and the KSVC pinned to that digest could no longer pull on a node change;
- the **newest** ``gc_keep_builds`` build tags beyond all of those;
- any tag the listing reports **without a digest**, which cannot be proven safe;
- everything, when the Image records **no successful build**.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from cloudlet_apis.logging import get_logger

from build_controller.config import BuildControllerSettings
from common import kpack
from common.labels import LABEL_WORKLOAD
from common.loop import PeriodicSweep
from common.names import digest_of, tag_of
from common.registry import RegistryClient, TagInfo, repository_path

logger = get_logger(__name__)


def garbage(
    tags: Iterable[TagInfo],
    *,
    protected_tags: set[str],
    protected_digests: set[str],
    keep: int,
) -> list[TagInfo]:
    """The tags nothing needs anymore - the pure half of the sweep.

    Protected tags are set aside *before* the newest-``keep`` window is cut, so
    ``keep`` counts only deletable build history and a protected tag never
    consumes a slot. A tag with no digest cannot be proven safe and is set aside
    the same way (docs/BUILD-CONTROLLER.md - Registry tag GC).

    Args:
        tags: The repository's active tags, in any order.
        protected_tags: Tag names that survive regardless of age or digest.
        protected_digests: Manifest digests whose every tag survives.
        keep: How many of the newest deletable tags survive beyond those.

    Returns:
        The tags to delete, newest first.
    """
    newest_first = sorted(tags, key=lambda t: (t.start_ts, t.name), reverse=True)
    deletable = [
        t
        for t in newest_first
        if t.name not in protected_tags and t.digest and t.digest not in protected_digests
    ]
    return deletable[keep:]


class TagGC(PeriodicSweep):
    """The periodic sweep: every function's repository, against live state.

    Constructed with the *resolved* region name - which cluster this controller
    actually watches - so the registry it prunes is the one that region's builds
    fill. It sweeps only when ``gc_enabled`` and
    ``registry.deleteOnFunctionDelete`` are both on, the registry has an API
    token, and no other region shares this registry's host. Whichever way that
    lands is logged at startup, and a blocking reason again once per interval,
    since one can be fixed under a running pod.

    The pacing and thread scaffolding live on :class:`common.loop.PeriodicSweep`:
    the sweep is registry-bound I/O that must never sit between the reconcile
    loop's relist and its watch, where every minute spent is a minute no digest
    rolls out (docs/BUILD-CONTROLLER.md - Registry tag GC).
    """

    label = "tag GC"

    def __init__(self, settings: BuildControllerSettings, region: str):
        """Resolve the region's registry and decide, audibly, whether to run.

        Args:
            settings: Controller settings (registry, regions, pacing, GC knobs).
            region: The resolved local region name.
        """
        super().__init__(settings.gc_interval_seconds, region, "tag-gc")
        self._registry = settings.registry_for(region)
        self._keep = settings.gc_keep_builds
        # Off by configuration: silent, logged once at startup.
        self._configured_off = not settings.gc_enabled
        # Blocked: loud - logged at startup and again once per interval.
        self._off_reason = self._blocked(settings, region)
        if self._configured_off:
            logger.info("tag GC off: disabled by configuration")
        elif self._off_reason:
            logger.warning("tag GC off: %s", self._off_reason)
        else:
            logger.info(
                "tag GC on: pruning old build tags in %s every %ds, "
                "keeping the newest %d build tag(s) per function",
                self._registry.host,
                self._interval,
                self._keep,
            )

    def _blocked(self, settings: BuildControllerSettings, region: str) -> str | None:
        """Why the GC must not delete anything here, or None to run.

        Args:
            settings: Controller settings, for the other regions' registries.
            region: The resolved local region name.

        Returns:
            An operator-readable reason, or None.
        """
        # One region per registry: a controller protects only its own region's
        # serving digest, so it refuses to sweep a registry another region
        # shares. The chart refuses to render such a config; this is the
        # backstop for a hand-rolled one.
        sharing = [
            other
            for other in settings.region_names
            if other != region and settings.registry_for(other).host == self._registry.host
        ]
        if sharing:
            return (
                f"region '{region}' shares registry {self._registry.host} with "
                f"{', '.join(sorted(sharing))}; every region must build into its own "
                "registry (docs/RUNTIMES.md - Registry layout), and pruning a shared one "
                "would delete tags a peer region still serves"
            )
        if not self._registry.delete_on_function_delete:
            return (
                "registry deletion is switched off (registry.deleteOnFunctionDelete); "
                "old build tags will accumulate until it is re-enabled"
            )
        if not self._registry.api_token:
            return (
                f"no registry API token for region '{region}' ({self._registry.host}); "
                "old build tags will accumulate until their functions are deleted. "
                "A token that appeared after startup needs a pod restart to be seen"
            )
        return None

    def enabled(self) -> bool:
        """Silent when the operator turned deletion off."""
        return not self._configured_off

    def blocked(self) -> str | None:
        """The loud off-reasons, re-said per interval (see the class docstring)."""
        return self._off_reason

    def sweep(self, images: Iterable[dict]) -> None:
        """Prune every function's repository, one connection for the lot.

        One function failing - a listing that times out, a transport error - is
        logged and skipped; the rest of the sweep continues.

        Args:
            images: The kpack Images to derive repositories and live state from.
        """
        started = time.monotonic()
        swept = pruned = failed = 0
        with RegistryClient(self._registry) as client:
            for image in images:
                try:
                    outcome = self._sweep_one(client, image)
                except Exception:  # noqa: BLE001 - the next function still gets its sweep
                    failed += 1
                    logger.exception(
                        "tag GC: sweeping '%s' failed; continuing with the rest",
                        _workload_of(image),
                    )
                    continue
                if outcome is None:
                    continue
                swept += 1
                pruned += outcome
        logger.info(
            "tag GC: swept %d function repositories in '%s', pruned %d tag(s), %d failed, in %.1fs",
            swept,
            self._region,
            pruned,
            failed,
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
        workload = _workload_of(image)
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
        digest = digest_of(latest)
        if not digest:
            # A fresh Image (created, re-created, or post-switchover) whose
            # status has not landed yet. The repository may still hold a
            # previous incarnation's tags, including the one still serving, and
            # with nothing digest-protected nothing here is prunable.
            logger.info(
                "tag GC: skipping '%s': its Image records no successful build yet", workload
            )
            return None
        protected_tags = {t for t in (tag_of(reference),) if t}
        tags = client.list_tags(repo)
        doomed = garbage(
            tags,
            protected_tags=protected_tags,
            protected_digests={digest},
            keep=self._keep,
        )
        if not doomed:
            return 0
        # Each delete logs its own tag name in RegistryClient; this line is the
        # per-function verdict.
        deleted = sum(1 for tag in doomed if client.delete_tag(repo, tag.name))
        logger.info(
            "tag GC: '%s': pruned %d of %d tag(s) in '%s' (kept the revision tag, "
            "the digest still serving, and the %d newest build tag(s))",
            workload,
            deleted,
            len(tags),
            repo,
            self._keep,
        )
        return deleted


def _workload_of(image: dict) -> str:
    """The workload an Image belongs to, for log lines; its name as fallback."""
    metadata = image.get("metadata") or {}
    return (metadata.get("labels") or {}).get(LABEL_WORKLOAD) or metadata.get("name", "?")
