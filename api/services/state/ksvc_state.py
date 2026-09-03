"""Reading state out of Knative objects the caller already holds.

Pure: every function here takes a Kubernetes object as a plain dict and returns
a value. Nothing reaches a cluster - :mod:`api.services.regions.region_read`
fetches, this module interprets what was fetched.

The dicts are API responses, so every level is read defensively (:func:`dig`):
a Knative object that has not been reconciled yet is missing most of what a
reconciled one has, and that is normal, not an error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from api.models.common import BuildStatusView, RegionStatus

# Israel local time, DST applied from the IANA database. `tzdata` is a
# dependency so this resolves in slim containers with no system zoneinfo.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def dig(obj: dict, *path: str, default=None):
    """Walk a nested dict by ``path``, treating a missing/None level as absent.

    A level that is missing, None, or not a dict stops the walk and yields
    ``default``.

    Args:
        obj: The dict to walk.
        path: The successive keys to follow.
        default: Returned if any level is missing or not a dict.

    Returns:
        The nested value, or ``default``.
    """
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def with_build_status(overall: str, build: BuildStatusView | None) -> str:
    """Fold a function's build state into the KSVC rollup, build first.

    The build is checked before the KSVC: a running build reports ``Building``
    and a failed build reports ``Failed`` whatever the KSVC says, and any other
    build state hands the verdict back to ``overall``. The phase set stays
    closed - the caller names the cause on ``reason`` ("BuildFailed",
    authoritative from the kpack Image) with the build's own text on
    ``message`` (docs/FUNCTIONS.md - Function Status Resolution).

    Args:
        overall: The rollup of the per-region KSVC statuses.
        build: The local region's build status, or None if it has no build.

    Returns:
        The status to report.
    """
    if build is None:
        return overall
    if build.state == "Building":
        return "Building"
    if build.state == "Failed":
        return "Failed"
    return overall


def roll_up_builds(builds: Iterable[BuildStatusView | None]) -> BuildStatusView | None:
    """Collapse the per-region build states into the one the workload reports.

    Every region builds its own copy (docs/BUILDING.md - Active/Active Behaviour), so a
    workload has one build state per region. A failed build anywhere wins and carries its
    own message; failing that, a build still running anywhere; failing that, the first
    state seen (docs/FUNCTIONS.md - Function Status Resolution).

    Args:
        builds: Each region's build status; None where a region has no build.

    Returns:
        The status to report, or None when no region has a build at all.
    """
    seen = [b for b in builds if b is not None]
    if not seen:
        return None
    return (
        next((b for b in seen if b.state == "Failed"), None)
        or next((b for b in seen if b.state == "Building"), None)
        or seen[0]
    )


def regions_with_build_status(
    regions: list[RegionStatus], builds: Mapping[str, BuildStatusView | None]
) -> list[RegionStatus]:
    """Apply the build-first rule to the per-region rows as well as the rollup.

    :func:`with_build_status` folds the headline; this folds the rows the detail view
    shows underneath it. Each row is folded against **its own** region's build, looked
    up by region name (docs/FUNCTIONS.md - Function Status Resolution):

    - ``Failed`` row, ``Building`` build: the row becomes ``Building`` with ``reason``
      and ``message`` cleared - the pull error is the running build, and a ``reason``
      left on the row is what the headline promotes.
    - ``Failed`` row, ``Failed`` build: the row stays ``Failed`` and names the cause,
      ``reason: "BuildFailed"`` with the build's own text as the message.
    - Any other row is passed through unchanged.

    Args:
        regions: The per-region statuses read from the KSVCs.
        builds: Each region's build status, keyed by region name.

    Returns:
        The per-region statuses to report.
    """
    out = []
    for region in regions:
        build = builds.get(region.region)
        if region.status == "Failed" and build is not None and build.state == "Building":
            out.append(
                region.model_copy(update={"status": "Building", "reason": None, "message": None})
            )
        elif region.status == "Failed" and build is not None and build.state == "Failed":
            out.append(
                region.model_copy(
                    update={"reason": "BuildFailed", "message": build.message or region.message}
                )
            )
        else:
            out.append(region)
    return out


# What each STATUS_REASONS value looks like in a failing condition's
# reason/message, matched case-insensitively. Kubernetes reason codes
# (ImagePullBackOff, CreateContainerConfigError) are stable; the free-text
# needles cover what Knative folds them into. Ordered specific-first:
# ProgressDeadlineExceeded is the aggregate verdict Knative reaches *because*
# of one of the others, so it only wins when nothing more specific matched.
_REASON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ImagePullFailed",
        (
            "imagepullbackoff",
            "errimagepull",
            "unable to fetch image",
            "failed to resolve image",
            "resolutionfailed",
            "pull access denied",
            "manifest unknown",
        ),
    ),
    (
        "ConfigError",
        ("createcontainerconfigerror", "couldn't find key", "mountvolume.setup failed"),
    ),
    (
        "CrashLooping",
        ("crashloopbackoff", "container failed", "back-off restarting", "exit code", "exitcode"),
    ),
    ("ProgressDeadlineExceeded", ("progressdeadlineexceeded", "did not become ready")),
)


def failure_cause(rev: dict | None, ksvc: dict | None = None) -> str | None:
    """Map failing conditions to a machine-readable cause (``STATUS_REASONS``).

    Joins the ``reason`` and ``message`` of every condition whose status is ``False``
    and matches ``_REASON_RULES`` against the result. Best-effort: the codes it reads
    are not a contract, so an unrecognized failure returns None and the caller reports
    the raw ``message`` text alone (docs/FUNCTIONS.md - Function Status Resolution).
    The Revision is scanned before the KSVC: its sub-conditions name the real cause
    where the KSVC's aggregate repeats the verdict.

    Args:
        rev: The failing Revision, when one was read.
        ksvc: The KSVC, as a fallback source of conditions.

    Returns:
        One of ``api.models.common.STATUS_REASONS``, or None.
    """
    texts: list[str] = []
    for obj in (rev, ksvc):
        if not obj:
            continue
        for cond in dig(obj, "status", "conditions", default=[]) or []:
            if cond.get("status") == "False":
                texts.append(str(cond.get("reason") or ""))
                texts.append(str(cond.get("message") or ""))
    blob = " ".join(texts).lower()
    for cause, needles in _REASON_RULES:
        if any(needle in blob for needle in needles):
            return cause
    return None


def extract_image(obj: dict) -> str | None:
    """The first container image of a KSVC, or None if absent."""
    containers = dig(obj, "spec", "template", "spec", "containers", default=[]) or []
    return containers[0].get("image") if containers else None


def creation_time(obj: dict) -> datetime | None:
    """The workload's creation time (`metadata.creationTimestamp`) in Israel time."""
    ts = dig(obj, "metadata", "creationTimestamp")
    # A missing or non-string timestamp has no valid parse, so the only error the
    # try below has to catch is fromisoformat's ValueError.
    if not isinstance(ts, str):
        return None
    try:
        # Kubernetes stamps RFC3339 UTC; present it in Israel local time.
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ISRAEL_TZ)
    except ValueError:
        return None


def ksvc_status(obj: dict) -> tuple[str, str | None]:
    """Map a KSVC's Ready condition to a (status, revision) pair.

    Returns:
        ``("Ready"|"Failed"|"Deploying"|"Terminating", revision_name_or_None)``.
    """
    status = dig(obj, "status", default={}) or {}
    conditions = status.get("conditions", []) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    revision = status.get("latestReadyRevisionName") or status.get("latestCreatedRevisionName")
    # A deletionTimestamp means the KSVC is being garbage-collected: report it as
    # Terminating so a GET during the delete window doesn't misreport it as Ready.
    if dig(obj, "metadata", "deletionTimestamp"):
        return "Terminating", revision
    # True = Ready, False = terminal failure, Unknown/absent = progressing.
    # ``Failed`` is terminal, so a poller stops on it.
    state = (ready or {}).get("status")
    if state == "True":
        return "Ready", revision
    if state == "False":
        return "Failed", revision
    return "Deploying", revision


def ksvc_failure_message(obj: dict) -> str | None:
    """The Knative Ready condition's failure reason/message, when it failed.

    Returns the human-readable ``message`` (falling back to the ``reason`` code)
    of a KSVC's ``Ready`` condition when its status is ``False`` - the rollout
    failure detail (RevisionFailed, image-pull error, ...) to surface as the
    per-region ``error``. None when Ready isn't False or carries no detail.
    """
    conditions = dig(obj, "status", "conditions", default=[]) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    if not ready or ready.get("status") != "False":
        return None
    return ready.get("message") or ready.get("reason") or None


def revision_replicas(rev: dict | None) -> int | None:
    """The autoscaler's live scale (``Revision.status.actualReplicas``), or None."""
    return dig(rev, "status", "actualReplicas") if rev else None


def revision_failure_message(rev: dict | None) -> str | None:
    """The most specific failure detail from a Revision's conditions, if failing.

    A Revision reports the aggregate ``Ready`` condition plus the sub-conditions
    feeding it. A failing sub-condition names the real cause (image pull, crash,
    quota), so it is preferred over the generic aggregate; then any failing
    condition's message, then its reason code.
    """
    conditions = dig(rev, "status", "conditions", default=[]) or []
    failing = [c for c in conditions if c.get("status") == "False"]
    if not failing:
        return None
    specific = next((c for c in failing if c.get("type") != "Ready" and c.get("message")), None)
    chosen = specific or next((c for c in failing if c.get("message")), None)
    if chosen is not None:
        return chosen.get("message")
    return next((c.get("reason") for c in failing if c.get("reason")), None)
