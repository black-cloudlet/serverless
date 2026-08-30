"""Merging a group's per-region listings into one summary per workload.

Pure dict-in, model-out: no clusters, no fan-out. That is the point of the merge
being its own module - the rules for what a partially-deployed workload reads as
are the interesting part, and they are now cheap to state.
"""

from api.models.common import ANNOTATION_HOST, ANNOTATION_SIZE, BuildStatusView
from api.services.state.summaries import merge

DOMAIN = "serverless.example.com"


def _ksvc(oname, *, ready=True, host=None, size="small", created="2026-06-21T15:00:00Z"):
    annotations = {ANNOTATION_SIZE: size}
    if host:
        annotations[ANNOTATION_HOST] = host
    return {
        "metadata": {"name": oname, "annotations": annotations, "creationTimestamp": created},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _merge(results, **kw):
    return merge(
        results,
        group=kw.pop("group", "team"),
        offering=kw.pop("offering", "container"),
        builds=kw.pop("builds", {}),
        route_domain=DOMAIN,
        **kw,
    )


def test_one_workload_across_two_regions_is_merged_into_one_row():
    out = _merge([("central", [_ksvc("app")]), ("south", [_ksvc("app")])])
    assert len(out) == 1
    assert out[0].name == "app"  # the -{group} suffix is the object name, not the display one
    assert out[0].regions == ["central", "south"]
    assert out[0].status == "Ready"


def test_a_workload_on_one_region_of_two_is_ready_not_degraded():
    """Its rollup covers the regions that returned it, not the regions that exist."""
    out = _merge([("central", [_ksvc("app")]), ("south", [])])
    assert out[0].regions == ["central"]
    assert out[0].status == "Ready"


def test_a_region_that_did_not_answer_is_skipped_entirely():
    out = _merge([("central", [_ksvc("app")]), ("south", None)])
    assert out[0].regions == ["central"]
    assert out[0].status == "Ready"


def test_a_failing_region_degrades_the_rollup():
    out = _merge([("central", [_ksvc("app")]), ("south", [_ksvc("app", ready=False)])])
    assert out[0].status == "Failed"


def test_a_running_build_wins_over_the_ksvc_status():
    # The KSVC cannot pull an image kpack has not pushed yet; that is not a failure.
    out = _merge(
        [("central", [_ksvc("fn", ready=False)])],
        offering="function",
        builds={"central": {"fn": BuildStatusView(state="Building")}},
    )
    assert out[0].status == "Building"


def test_a_build_failing_in_one_region_is_what_the_listing_reports():
    """Rolled up across regions: Ready in one is not the answer when another failed."""
    out = _merge(
        [("central", [_ksvc("fn")]), ("south", [_ksvc("fn")])],
        offering="function",
        builds={
            "central": {"fn": BuildStatusView(state="Ready")},
            "south": {"fn": BuildStatusView(state="Failed", message="detect failed")},
        },
    )
    assert out[0].status == "Failed"


def test_a_build_state_is_not_attributed_across_regions():
    """A region that returned the workload but has no build of it contributes None."""
    out = _merge(
        [("central", [_ksvc("fn", ready=False)]), ("south", [_ksvc("fn")])],
        offering="function",
        builds={"central": {"fn": BuildStatusView(state="Building")}, "south": {}},
    )
    assert out[0].status == "Building"


def test_the_host_falls_back_to_the_default_when_unannotated():
    out = _merge([("central", [_ksvc("app")])])
    assert out[0].hostname == f"app-team.{DOMAIN}"

    out = _merge([("central", [_ksvc("app", host="custom.example.com")])])
    assert out[0].hostname == "custom.example.com"


def test_sorting_by_name_and_by_creation():
    older = _ksvc("a", created="2026-01-01T00:00:00Z")
    newer = _ksvc("b", created="2026-09-09T00:00:00Z")
    results = [("central", [newer, older])]

    assert [w.name for w in _merge(results)] == ["a", "b"]
    assert [w.name for w in _merge(results, sort="createdAt")] == ["a", "b"]


def test_a_workload_with_no_creation_time_sorts_last():
    dated = _ksvc("a", created="2026-01-01T00:00:00Z")
    undated = _ksvc("b", created=None)
    out = _merge([("central", [undated, dated])], sort="createdAt")
    assert [w.name for w in out] == ["a", "b"]
    assert out[1].createdAt is None


def test_nothing_deployed_anywhere_is_an_empty_list():
    assert _merge([("central", []), ("south", None)]) == []


def test_a_name_that_ends_in_the_group_is_not_shortened():
    """The listing used to strip a "-{group}" suffix to recover a display name.

    Object names are plain now, so that strip renames anyone unlucky enough to
    end in their own group: `api-team` in group `team` would list as `api`, and
    the GET the caller made from that listing would 404.
    """
    ksvc = {
        "metadata": {
            "name": "api-team",
            "labels": {"serverless.platform/group": "team"},
            "creationTimestamp": "2026-08-05T12:00:00Z",
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }

    rows = merge(
        [("region-a", [ksvc])],
        group="team",
        offering="container",
        builds={},
        route_domain="serverless.example.com",
        sort="name",
    )

    assert [r.name for r in rows] == ["api-team"]
