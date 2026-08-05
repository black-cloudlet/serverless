"""Merge a group's per-site KSVC listings into one summary per workload."""

from __future__ import annotations

from datetime import datetime, timezone

from api.models.common import (
    ANNOTATION_HOST,
    ANNOTATION_SIZE,
    BuildStatusView,
    WorkloadSummary,
)
from api.services.manifests import route as route_svc
from api.services.sites.deployer import overall_status
from api.services.state import ksvc_state

# createdAt is optional, so sort Nones last rather than letting a comparison fail.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def merge(
    results: list[tuple[str, list[dict] | None]],
    *,
    group: str,
    offering: str,
    builds: dict[str, BuildStatusView],
    route_domain: str,
    sort: str = "name",
) -> list[WorkloadSummary]:
    """One summary per workload, merged across the sites that returned it."""
    suffix = f"-{group}"
    merged: dict[str, dict] = {}
    for site, items in results:
        if items is None:
            continue
        for obj in items:
            meta = obj.get("metadata", {}) or {}
            oname = meta.get("name", "")
            # object name is "{name}-{group}"; recover the display name
            name = oname[: -len(suffix)] if oname.endswith(suffix) else oname
            annotations = meta.get("annotations", {}) or {}
            status, _ = ksvc_state.ksvc_status(obj)
            entry = merged.setdefault(
                name,
                {
                    # the object name, which is what a build state is keyed by
                    "oname": oname,
                    "host": None,
                    "size": None,
                    "createdAt": None,
                    "sites": [],
                    "statuses": [],
                },
            )
            entry["host"] = entry["host"] or annotations.get(ANNOTATION_HOST)
            entry["size"] = entry["size"] or annotations.get(ANNOTATION_SIZE)
            entry["createdAt"] = entry["createdAt"] or ksvc_state.creation_time(obj)
            entry["sites"].append(site)
            entry["statuses"].append(status)

    summaries = [
        WorkloadSummary(
            name=name,
            group=group,
            type=offering,
            hostname=entry["host"] or route_svc.host_for(name, group, route_domain),
            overallStatus=ksvc_state.with_build_status(
                overall_status(entry["statuses"]), builds.get(entry["oname"])
            ),
            size=entry["size"],
            createdAt=entry["createdAt"],
            sites=sorted(entry["sites"]),
        )
        for name, entry in merged.items()
    ]
    if sort == "createdAt":
        summaries.sort(key=lambda w: (w.createdAt is None, w.createdAt or _EPOCH))
    else:
        summaries.sort(key=lambda w: w.name)
    return summaries
