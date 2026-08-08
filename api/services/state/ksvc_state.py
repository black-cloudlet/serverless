"""Reading state out of Knative objects the caller already holds.

Pure: every function here takes a Kubernetes object as a plain dict and returns
a value. Nothing reaches a cluster, which is what separates this module from
:mod:`api.services.sites.site_read` - that one fetches, this one interprets what was
fetched. It is also why these are the cheapest rules in the service to test:
hand them a dict, assert on the answer.

The dicts are API responses, so every level is read defensively (:func:`dig`) -
a Knative object that has not been reconciled yet is missing most of what a
reconciled one has, and that is normal, not an error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from api.models.common import BuildStatusView, SiteStatus

# Israel local time, DST applied from the IANA database. `tzdata` is a
# dependency so this resolves in slim containers with no system zoneinfo.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def dig(obj: dict, *path: str, default=None):
    """Walk a nested dict by ``path``, treating a missing/None level as absent.

    Replaces the repeated ``(d.get(k, {}) or {})`` chains used to read Kubernetes
    objects defensively.

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
    """Fold a function's build state into the KSVC rollup (docs/FUNCTIONS.md).

    The build is checked FIRST: a function whose image does not exist yet is not
    broken, but its KSVC is failing to pull one, which would read as ``Degraded`` for
    a whole normal first build. A failed build is the honest cause of that same
    symptom, so it does report ``Degraded``, with the reason on ``build.message``.

    Args:
        overall: The rollup of the per-site KSVC statuses.
        build: The local site's build status, or None if it has no build.

    Returns:
        The status to report.
    """
    if build is None:
        return overall
    if build.state == "Building":
        return "Building"
    if build.state == "Failed":
        return "Degraded"
    return overall


def roll_up_builds(builds: Iterable[BuildStatusView | None]) -> BuildStatusView | None:
    """Collapse the per-site build states into the one the workload reports.

    Every site builds its own copy (docs/PER-SITE-REGISTRY.md), so "the
    function's build" is no longer a single thing. A failure anywhere wins, and
    carries its own message: it is the actionable state, and reporting ``Ready``
    because the other site managed it would hide the site that did not. Failing
    that, a build still running anywhere means the rollout is not finished.

    Args:
        builds: Each site's build status; None where a site has no build.

    Returns:
        The status to report, or None when no site has a build at all.
    """
    seen = [b for b in builds if b is not None]
    if not seen:
        return None
    return (
        next((b for b in seen if b.state == "Failed"), None)
        or next((b for b in seen if b.state == "Building"), None)
        or seen[0]
    )


def sites_with_build_status(
    sites: list[SiteStatus], builds: Mapping[str, BuildStatusView | None]
) -> list[SiteStatus]:
    """Apply the build-first rule to the per-site rows, not just the rollup.

    :func:`with_build_status` fixes the headline while a build runs, but each site
    row is read straight off its KSVC - and that KSVC is failing to pull an image
    kpack has not pushed yet. Left alone the detail view says ``Building`` at the
    top and ``Failed`` - ``Unable to fetch image ...`` in the sites table
    immediately below it, which reads as a broken deploy during what is a normal
    first build.

    So while a site's build is in flight, that site reports ``Building`` and
    drops the pull error: it is a symptom of the build running there, not an
    independent failure. Only ``Building`` masks anything - a ``Failed`` build
    leaves the row untouched, because then the image genuinely will not arrive
    and the site is telling the truth.

    Each row is folded against **its own** site's build. A build running in one
    site says nothing about whether another site's image exists, so a shared
    verdict would mask a real failure next to a healthy neighbour.

    Args:
        sites: The per-site statuses read from the KSVCs.
        builds: Each site's build status, keyed by site name.

    Returns:
        The per-site statuses to report.
    """
    out = []
    for site in sites:
        build = builds.get(site.site)
        if site.status == "Failed" and build is not None and build.state == "Building":
            out.append(site.model_copy(update={"status": "Building", "error": None}))
        else:
            out.append(site)
    return out


def extract_image(obj: dict) -> str | None:
    """The first container image of a KSVC, or None if absent."""
    containers = dig(obj, "spec", "template", "spec", "containers", default=[]) or []
    return containers[0].get("image") if containers else None


def creation_time(obj: dict) -> datetime | None:
    """The workload's creation time (`metadata.creationTimestamp`) in Israel time."""
    ts = dig(obj, "metadata", "creationTimestamp")
    # A non-string (or missing) timestamp has no valid parse; str keeps the
    # try to just the fromisoformat ValueError (avoids a multi-except tuple).
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
    # The False/Unknown split is what lets a poller stop instead of spinning.
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
    per-site ``error``. None when Ready isn't False or carries no detail.
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
