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
its site filled, and no cross-site call exists. That premise is load-bearing,
so it is *checked*: every site must build into its own registry (the chart
refuses to render otherwise), and a controller that finds another site on its
registry host refuses to sweep - two sites pruning one repository would each
protect only their own serving digest and delete the other's
(docs/PER-SITE-REGISTRY.md). And *reconciled*, unlike the API's fire-once
cleanup on delete: garbage is re-derived from live state every sweep, so a
crash or an unreachable registry leaks nothing permanently - the next sweep
collects it.

What survives a sweep (:func:`garbage`), and why deleting the rest is safe:

- the function's **current branch tag** (the tag half of ``Image.spec.tag``) -
  a create deploys at it, and a switchover site rebuilds into it;
- every tag still pointing at the **digest of** ``status.latestImage`` -
  deleting the last tag on a manifest lets the registry collect the manifest,
  and the KSVC pinned to that digest could no longer pull on a node change;
- the **newest** ``gc_keep_builds`` build tags beyond all of those, so recent
  builds stay addressable, mirroring the Build history kpack itself keeps;
- any tag the listing reports **without a digest** - it cannot be proven safe;
- everything, when the Image records **no successful build**: a fresh Image
  over a repository still holding a previous incarnation's tags would
  otherwise be swept with nothing digest-protected at all.

An older *revision* can pin a digest outside that set; Quay's time machine is
the accepted backstop for one that re-pulls after its tags are pruned
(docs/BUILDING.md - Registry tag GC).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from cloudlet_apis.logging import get_logger

from common import kpack
from common.labels import LABEL_WORKLOAD
from common.names import digest_of, tag_of
from common.registry import RegistryClient, TagInfo, repository_path
from controller.config import ControllerSettings

logger = get_logger(__name__)


def garbage(
    tags: Iterable[TagInfo],
    *,
    protected_tags: set[str],
    protected_digests: set[str],
    keep: int,
) -> list[TagInfo]:
    """The tags nothing needs anymore - the pure half of the sweep.

    Every protected tag is set aside *before* the newest-``keep`` window is
    cut, so the window is spent entirely on deletable build history: the
    branch tag is re-pushed by every build and the newest build tag always
    carries the serving digest, so counting either would silently shrink the
    retained history below what ``keep`` promises. Tags without a digest are
    set aside the same way - they cannot be proven safe, and the safe
    direction is to keep them.

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


class TagGC:
    """The periodic sweep: every function's repository, against live state.

    Constructed with the *resolved* site name - which cluster this controller
    actually watches - so the registry it prunes is the one that site's builds
    fill. It runs only when configuration allows deletion (``gc_enabled`` AND
    ``registry.deleteOnFunctionDelete`` - the operator's one switch for "may
    the platform delete registry content"), the registry has an API token, and
    no other site shares this registry's host. Whichever way that lands, it is
    said at startup - and, for a reason that could change under a running pod
    (a token that syncs late needs a pod restart to be seen), repeated once
    per interval - so an operator reads the state off the log instead of
    deducing it from silence.

    The sweep itself runs on its own daemon thread: it is registry-bound I/O
    that must never sit between the reconcile loop's relist and its watch,
    where every minute spent is a minute no digest rolls out.
    """

    def __init__(self, settings: ControllerSettings, site: str):
        """Resolve the site's registry and decide, audibly, whether to run.

        Args:
            settings: Controller settings (registry, sites, pacing, GC knobs).
            site: The resolved local site name.
        """
        self._site = site
        self._registry = settings.registry_for(site)
        self._keep = settings.gc_keep_builds
        self._interval = settings.gc_interval_seconds
        # Silent off: the operator asked for no GC, once at startup is enough.
        self._configured_off = not settings.gc_enabled
        # Loud off: a state the operator likely wants fixed, re-said per interval.
        self._off_reason = self._blocked(settings, site)
        # Monotonic deadline; zero means the first resync sweeps immediately,
        # so a restarted controller shows its GC working within one pass.
        self._next_sweep = 0.0
        self._thread: threading.Thread | None = None
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

    def _blocked(self, settings: ControllerSettings, site: str) -> str | None:
        """Why the GC must not delete anything here, or None to run.

        Args:
            settings: Controller settings, for the other sites' registries.
            site: The resolved local site name.

        Returns:
            An operator-readable reason, or None.
        """
        # One site per registry is the safety premise: two controllers pruning
        # one repository each protect only their own serving digest and delete
        # the other's. The chart enforces it at render; this is the backstop
        # for a hand-rolled config.
        sharing = [
            other
            for other in settings.site_names
            if other != site and settings.registry_for(other).host == self._registry.host
        ]
        if sharing:
            return (
                f"site '{site}' shares registry {self._registry.host} with "
                f"{', '.join(sorted(sharing))}; every site must build into its own "
                "registry (docs/PER-SITE-REGISTRY.md), and pruning a shared one "
                "would delete tags a peer site still serves"
            )
        if not self._registry.delete_on_function_delete:
            return (
                "registry deletion is switched off (registry.deleteOnFunctionDelete); "
                "old build tags will accumulate until it is re-enabled"
            )
        if not self._registry.api_token:
            return (
                f"no registry API token for site '{site}' ({self._registry.host}); "
                "old build tags will accumulate until their functions are deleted. "
                "A token that appeared after startup needs a pod restart to be seen"
            )
        return None

    def maybe_sweep(self, images: list[dict]) -> None:
        """Start a sweep when due, and never raise into the reconcile loop.

        Called with each resync's listing, so being due costs no second LIST.
        The sweep runs on its own daemon thread - the reconcile loop's job,
        rolling out digests, must not wait on registry I/O - and the next
        deadline is set when one *starts*, so a failing registry retries at
        the next due resync rather than every resync.

        Args:
            images: The kpack Images the resync just listed.
        """
        if self._configured_off:
            return
        now = time.monotonic()
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._interval
        if self._off_reason:
            logger.warning("tag GC off: %s", self._off_reason)
            return
        if self._thread is not None and self._thread.is_alive():
            # A sweep outliving its own interval is a registry problem worth a
            # line; starting a second would race the first over the same tags.
            logger.warning(
                "tag GC: previous sweep in '%s' still running after %ds; not starting another",
                self._site,
                self._interval,
            )
            return
        self._thread = threading.Thread(
            target=self._run, args=(list(images),), name="tag-gc", daemon=True
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running sweep (if any) finishes.

        For tests and for anyone sequencing against the sweep; the loop itself
        never calls it.

        Args:
            timeout: Seconds to wait at most, or None for as long as it takes.
        """
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, images: list[dict]) -> None:
        """The thread body: one sweep, with the loop's no-raise guarantee."""
        try:
            self.sweep(images)
        except Exception:  # noqa: BLE001 - a failed sweep is logged, not the loop's end
            logger.exception(
                "tag GC: sweep failed in '%s'; retrying in ~%ds", self._site, self._interval
            )

    def sweep(self, images: Iterable[dict]) -> None:
        """Prune every function's repository, one connection for the lot.

        One function failing - a listing that times out, a transport error -
        is logged and skipped, never the end of the sweep: the Image listing
        order is stable, so an error that aborted the pass would starve every
        function after the failing one on every sweep, deterministically.

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
            self._site,
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
            # status has not landed yet - but the repository may still hold a
            # previous incarnation's tags, including the one still serving.
            # With nothing digest-protected, pruning would be a guess.
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


def _workload_of(image: dict) -> str:
    """The workload an Image belongs to, for log lines; its name as fallback."""
    metadata = image.get("metadata") or {}
    return (metadata.get("labels") or {}).get(LABEL_WORKLOAD) or metadata.get("name", "?")
