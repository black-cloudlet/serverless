"""Reading state out of Knative objects the caller already holds."""

from __future__ import annotations

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
    """Fold a function's build state into the KSVC rollup (docs/FUNCTIONS.md)."""
    if build is None:
        return overall
    if build.state == "Building":
        return "Building"
    if build.state == "Failed":
        return "Degraded"
    return overall


def sites_with_build_status(
    sites: list[SiteStatus], build: BuildStatusView | None
) -> list[SiteStatus]:
    """Apply the build-first rule to the per-site rows, not just the rollup."""
    if build is None or build.state != "Building":
        return sites
    return [
        s.model_copy(update={"status": "Building", "error": None}) if s.status == "Failed" else s
        for s in sites
    ]


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
    """Map a KSVC's Ready condition to a (status, revision) pair."""
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
    """The Knative Ready condition's failure reason/message, when it failed."""
    conditions = dig(obj, "status", "conditions", default=[]) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    if not ready or ready.get("status") != "False":
        return None
    return ready.get("message") or ready.get("reason") or None


def revision_replicas(rev: dict | None) -> int | None:
    """The autoscaler's live scale (``Revision.status.actualReplicas``), or None."""
    return dig(rev, "status", "actualReplicas") if rev else None


def revision_failure_message(rev: dict | None) -> str | None:
    """The most specific failure detail from a Revision's conditions, if failing."""
    conditions = dig(rev, "status", "conditions", default=[]) or []
    failing = [c for c in conditions if c.get("status") == "False"]
    if not failing:
        return None
    specific = next((c for c in failing if c.get("type") != "Ready" and c.get("message")), None)
    chosen = specific or next((c for c in failing if c.get("message")), None)
    if chosen is not None:
        return chosen.get("message")
    return next((c.get("reason") for c in failing if c.get("reason")), None)
