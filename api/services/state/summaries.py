"""Merge a group's per-region KSVC listings into one summary per workload.

Pure, like :mod:`api.services.state.ksvc_state`: it takes what the fan-out already
fetched and returns the response objects. The listing's I/O - the fan-out and
the build-state read - stays in the engine, so the merge rules that decide what
a workload deployed to one region of two reads as are testable with plain dicts.

The merge is deliberately partial-tolerant. A region that did not answer is simply
absent from the input, and a workload's rollup covers only the regions that did
return it, so a single-region workload reads ``Ready`` rather than ``Failed``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from api.models.common import (
    ANNOTATION_HOST,
    ANNOTATION_SIZE,
    BuildStatusView,
    WorkloadSummary,
)
from api.services.manifests import route as route_svc
from api.services.regions.rollup import overall_status
from api.services.state import ksvc_state

# createdAt is optional, so sort Nones last rather than letting a comparison fail.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def merge(
    results: list[tuple[str, list[dict] | None]],
    *,
    group: str,
    offering: str,
    builds: dict[str, dict[str, BuildStatusView]],
    route_domain: str,
    sort: str = "name",
) -> list[WorkloadSummary]:
    """One summary per workload, merged across the regions that returned it.

    Args:
        results: ``(region, ksvcs_or_None)`` per region; None means it did not answer.
        group: The owning group.
        offering: The offering being listed ("function"/"container").
        builds: Build states per region (``{region: {object_name: state}}``), for
            the build-first rollup. Each region builds its own copy, so a
            workload's state is rolled up across the regions that returned it.
            Empty for an offering with no build.
        route_domain: Used to derive a host for a workload whose KSVC carries no
            host annotation.
        sort: "name" or "createdAt".

    Returns:
        The sorted summaries.
    """
    merged: dict[str, dict] = {}
    for region, items in results:
        if items is None:
            continue
        for obj in items:
            meta = obj.get("metadata", {}) or {}
            # The object name IS the workload name now - the namespace carries
            # the group. Stripping a "-{group}" suffix here would rename a
            # workload that happens to end in one: `api-team` in group `team`
            # would list as `api`, and the GET that followed would 404.
            name = meta.get("name", "")
            annotations = meta.get("annotations", {}) or {}
            status, _ = ksvc_state.ksvc_status(obj)
            entry = merged.setdefault(
                name,
                {
                    "host": None,
                    "size": None,
                    "createdAt": None,
                    "regions": [],
                    "statuses": [],
                    "builds": [],
                },
            )
            entry["host"] = entry["host"] or annotations.get(ANNOTATION_HOST)
            entry["size"] = entry["size"] or annotations.get(ANNOTATION_SIZE)
            entry["createdAt"] = entry["createdAt"] or ksvc_state.creation_time(obj)
            entry["regions"].append(region)
            entry["statuses"].append(status)
            entry["builds"].append(builds.get(region, {}).get(name))

    summaries = [
        WorkloadSummary(
            name=name,
            group=group,
            type=offering,
            hostname=entry["host"] or route_svc.host_for(name, group, route_domain),
            status=ksvc_state.with_build_status(
                overall_status(entry["statuses"]), ksvc_state.roll_up_builds(entry["builds"])
            ),
            size=entry["size"],
            createdAt=entry["createdAt"],
            regions=sorted(entry["regions"]),
        )
        for name, entry in merged.items()
    ]
    if sort == "createdAt":
        summaries.sort(key=lambda w: (w.createdAt is None, w.createdAt or _EPOCH))
    else:
        summaries.sort(key=lambda w: w.name)
    return summaries
