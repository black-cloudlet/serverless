"""Merging a group's per-site listings into one summary per workload.

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


def test_one_workload_across_two_sites_is_merged_into_one_row():
    out = _merge([("central", [_ksvc("app-team")]), ("south", [_ksvc("app-team")])])
    assert len(out) == 1
    assert out[0].name == "app"  # the -{group} suffix is the object name, not the display one
    assert out[0].sites == ["central", "south"]
    assert out[0].overallStatus == "Ready"


def test_a_workload_on_one_site_of_two_is_ready_not_degraded():
    """Its rollup covers the sites that returned it, not the sites that exist."""
    out = _merge([("central", [_ksvc("app-team")]), ("south", [])])
    assert out[0].sites == ["central"]
    assert out[0].overallStatus == "Ready"


def test_a_site_that_did_not_answer_is_skipped_entirely():
    out = _merge([("central", [_ksvc("app-team")]), ("south", None)])
    assert out[0].sites == ["central"]
    assert out[0].overallStatus == "Ready"


def test_a_failing_site_degrades_the_rollup():
    out = _merge([("central", [_ksvc("app-team")]), ("south", [_ksvc("app-team", ready=False)])])
    assert out[0].overallStatus == "Degraded"


def test_a_running_build_wins_over_the_ksvc_status():
    # The KSVC cannot pull an image kpack has not pushed yet; that is not a failure.
    out = _merge(
        [("central", [_ksvc("fn-team", ready=False)])],
        offering="function",
        builds={"central": {"fn-team": BuildStatusView(state="Building")}},
    )
    assert out[0].overallStatus == "Building"


def test_a_build_failing_in_one_site_is_what_the_listing_reports():
    """Rolled up across sites: Ready in one is not the answer when another failed."""
    out = _merge(
        [("central", [_ksvc("fn-team")]), ("south", [_ksvc("fn-team")])],
        offering="function",
        builds={
            "central": {"fn-team": BuildStatusView(state="Ready")},
            "south": {"fn-team": BuildStatusView(state="Failed", message="detect failed")},
        },
    )
    assert out[0].overallStatus == "Degraded"


def test_a_build_state_is_not_attributed_across_sites():
    """A site that returned the workload but has no build of it contributes None."""
    out = _merge(
        [("central", [_ksvc("fn-team", ready=False)]), ("south", [_ksvc("fn-team")])],
        offering="function",
        builds={"central": {"fn-team": BuildStatusView(state="Building")}, "south": {}},
    )
    assert out[0].overallStatus == "Building"


def test_the_host_falls_back_to_the_default_when_unannotated():
    out = _merge([("central", [_ksvc("app-team")])])
    assert out[0].hostname == f"app-team.{DOMAIN}"

    out = _merge([("central", [_ksvc("app-team", host="custom.example.com")])])
    assert out[0].hostname == "custom.example.com"


def test_sorting_by_name_and_by_creation():
    older = _ksvc("a-team", created="2026-01-01T00:00:00Z")
    newer = _ksvc("b-team", created="2026-09-09T00:00:00Z")
    results = [("central", [newer, older])]

    assert [w.name for w in _merge(results)] == ["a", "b"]
    assert [w.name for w in _merge(results, sort="createdAt")] == ["a", "b"]


def test_a_workload_with_no_creation_time_sorts_last():
    dated = _ksvc("a-team", created="2026-01-01T00:00:00Z")
    undated = _ksvc("b-team", created=None)
    out = _merge([("central", [undated, dated])], sort="createdAt")
    assert [w.name for w in out] == ["a", "b"]
    assert out[1].createdAt is None


def test_nothing_deployed_anywhere_is_an_empty_list():
    assert _merge([("central", []), ("south", None)]) == []
