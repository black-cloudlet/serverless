"""The region layer: fan-out, the bounded read pool, and the status rollup."""

import asyncio

import pytest

from api.core.config import Settings
from api.models.common import RegionStatus
from api.services.regions.deployer import Deployer
from api.services.regions.rollup import aggregate, status_code_for
from common.errors import RegionTotalFailure, ValidationError
from tests.factories import settings_with_regions


def test_aggregate_all_ok():
    statuses = [RegionStatus(region="a", status="Ready"), RegionStatus(region="b", status="Ready")]
    assert aggregate(statuses) == "Ready"
    assert status_code_for("Ready", created=True) == 201


def test_aggregate_still_rolling_out():
    # a just-applied workload (regions not Ready yet) reports Deploying, not Ready
    statuses = [
        RegionStatus(region="a", status="Ready"),
        RegionStatus(region="b", status="Deploying"),
    ]
    assert aggregate(statuses) == "Deploying"


def test_aggregate_partial():
    statuses = [
        RegionStatus(region="a", status="Ready"),
        RegionStatus(region="b", status="Failed", message="boom"),  # unreachable -> Failed
    ]
    assert aggregate(statuses) == "Failed"
    assert status_code_for("Failed", created=True) == 207


def test_aggregate_total_failure():
    statuses = [RegionStatus(region="a", status="Failed", message="x")]
    with pytest.raises(RegionTotalFailure):
        aggregate(statuses)


def test_overall_status_rollup():
    from api.services.regions.rollup import overall_status

    assert overall_status(["Ready", "Ready"]) == "Ready"
    assert overall_status(["Deploying", "Deploying"]) == "Deploying"
    # a normal rollout where one region is ahead is still in-progress, not Failed
    assert overall_status(["Ready", "Deploying"]) == "Deploying"
    # any failed (or unreachable, mapped to Failed) region -> Failed
    assert overall_status(["Ready", "Failed"]) == "Failed"
    assert overall_status(["Deploying", "Failed"]) == "Failed"
    assert overall_status([]) == "Failed"


def test_stats_code_for_deploying_is_non_terminal():
    # Deploying is an accepted, still-rolling-out state, not a partial failure
    assert status_code_for("Deploying", created=True) == 202
    assert status_code_for("Deploying", created=False) == 202
    assert status_code_for("Ready", created=False) == 200


def test_ksvc_status_distinguishes_failed_from_deploying():
    from api.services.state.ksvc_state import ksvc_status

    def _obj(ready_status):
        conditions = [{"type": "Ready", "status": ready_status}] if ready_status else []
        return {"status": {"conditions": conditions, "latestCreatedRevisionName": "rev-1"}}

    assert ksvc_status(_obj("True")) == ("Ready", "rev-1")
    assert ksvc_status(_obj("False")) == ("Failed", "rev-1")  # terminal failure
    assert ksvc_status(_obj("Unknown")) == ("Deploying", "rev-1")  # still progressing
    assert ksvc_status(_obj(None)) == ("Deploying", "rev-1")  # no condition yet
    assert ksvc_status({}) == ("Deploying", None)  # brand-new, no status block


def test_global_cert_and_ca_paths():
    s = Settings(client_cert_dir="/etc/serverless/client")
    assert s.client_cert_file == "/etc/serverless/client/tls.crt"
    assert s.client_key_file == "/etc/serverless/client/tls.key"
    assert s.ca_bundle.file == "/etc/ssl/certs/ca-bundle.crt"


def test_resolve_targets_default_all():
    d = Deployer(settings_with_regions())
    assert [z.region for z in d.resolve_targets(None)] == ["region-a", "region-b"]


def test_resolve_targets_unknown_region():
    d = Deployer(settings_with_regions())
    with pytest.raises(ValidationError):
        d.resolve_targets(["region-c"])


def test_local_cluster_selection():
    d = Deployer(settings_with_regions())
    # unset -> first configured region
    assert d.local_cluster().region == "region-a"
    # match by region name
    d._local_region = "region-b"
    assert d.local_cluster().region == "region-b"
    # match by cluster name (Cluster.name), not just region name
    d._local_region = "region-b-0"
    assert d.local_cluster().region == "region-b"
    # unknown value -> an error, not a silent adoption of the first region: a
    # process serving as a region it is not would build and reconcile wrongly.
    d._local_region = "nope"
    with pytest.raises(ValidationError):
        d.local_cluster()


async def test_fanout_captures_per_region_errors():
    d = Deployer(settings_with_regions())

    def fn(cluster):
        if cluster.region == "region-b":
            raise RuntimeError("kaboom")
        return RegionStatus(region=cluster.region, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_region = {s.region: s for s in statuses}
    assert by_region["region-a"].status == "Ready"
    assert by_region["region-b"].message == "kaboom"


async def test_fanout_times_out_unreachable_region():
    import time

    d = Deployer(settings_with_regions())
    d._op_timeout = 0.05  # tighten for the test

    def fn(cluster):
        if cluster.region == "region-b":
            time.sleep(0.5)  # simulate an unreachable/slow cluster
        return RegionStatus(region=cluster.region, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_region = {s.region: s for s in statuses}
    # the healthy region still returns; the slow one is reported, not blocking
    assert by_region["region-a"].status == "Ready"
    assert by_region["region-b"].status == "Timeout"
    assert by_region["region-b"].message is not None


async def test_a_read_fanout_gives_up_on_a_slow_region_quickly():
    """A page read is bounded by cluster_read_op_timeout, not the minute-scale
    write backstop: a slow cluster costs its own column, never the page."""
    import time

    d = Deployer(settings_with_regions())
    d._read_timeout = 0.05
    d._op_timeout = 30.0  # the write backstop stays long; the read must not use it

    def fn(cluster):
        if cluster.region == "region-b":
            time.sleep(0.5)
        return RegionStatus(region=cluster.region, status="Ready")

    start = time.monotonic()
    statuses = await d.fanout(d.resolve_targets(None), fn, read=True)
    assert time.monotonic() - start < 0.4  # gave up at the read timeout, not the op one
    by_region = {s.region: s for s in statuses}
    assert by_region["region-a"].status == "Ready"
    assert by_region["region-b"].status == "Timeout"


async def test_gather_each_is_bounded_by_the_read_timeout():
    import time

    d = Deployer(settings_with_regions())
    d._read_timeout = 0.05

    def fn(cluster):
        if cluster.region == "region-b":
            time.sleep(0.5)
        return ["item"]

    start = time.monotonic()
    results = dict(await d.gather_each(d.resolve_targets(None), fn))
    assert time.monotonic() - start < 0.4
    assert results["region-a"] == ["item"]
    assert results["region-b"] is None  # skipped, not fatal


async def test_a_saturated_read_pool_refuses_with_503_not_failed_regions():
    """Pool saturation is the API's condition: the request 503s; it must not be
    dressed up as every region having failed (which would read as a 502)."""
    import threading

    from cloudlet_apis.errors import ServiceUnavailableError

    from api.services.regions.deployer import ReadPool, ReadPoolSaturated

    pool = ReadPool(workers=1, max_queued=0)
    release = threading.Event()
    try:
        blocker = asyncio.ensure_future(pool.run(release.wait))
        await asyncio.sleep(0.05)  # the one worker is now occupied
        with pytest.raises(ServiceUnavailableError):
            await pool.run(lambda: "refused")
        assert issubclass(ReadPoolSaturated, ServiceUnavailableError)
    finally:
        release.set()
        await blocker
        pool.shutdown()


async def test_a_region_function_raising_503_stays_a_region_failure():
    """Only the pool's own saturation escapes the fan-out; a region whose work
    raised ServiceUnavailableError is that region's failure row."""
    from cloudlet_apis.errors import ServiceUnavailableError

    d = Deployer(settings_with_regions())

    def fn(cluster):
        if cluster.region == "region-b":
            raise ServiceUnavailableError("registry down")
        return RegionStatus(region=cluster.region, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn, read=True)
    by_region = {s.region: s for s in statuses}
    assert by_region["region-a"].status == "Ready"
    assert by_region["region-b"].status == "Failed"
    assert "registry down" in by_region["region-b"].message


async def test_a_read_fanout_is_admitted_whole_or_not_at_all():
    """Admitted region by region, a fan-out refused half way through would leave
    the regions it had already started burning the pool for a result the 503
    throws away - gather cancels no siblings - which is how shedding feeds itself."""
    from cloudlet_apis.errors import ServiceUnavailableError

    from api.services.regions.deployer import ReadPool

    d = Deployer(settings_with_regions())
    d._read_pool.shutdown()
    d._read_pool = ReadPool(workers=1, max_queued=0)  # one slot; the fan-out needs two
    started = []

    def fn(cluster):
        started.append(cluster.region)
        return RegionStatus(region=cluster.region, status="Ready")

    try:
        with pytest.raises(ServiceUnavailableError):
            await d.fanout(d.resolve_targets(None), fn, read=True)
        assert started == []
        # ...and the refusal left nothing behind: the whole reservation is back.
        assert d._read_pool._inflight == 0
    finally:
        d._read_pool.shutdown()


async def test_a_single_read_is_bounded_by_the_read_timeout():
    """run_read is on the same bounded pool as the fan-outs, and a slot is only
    released when the THREAD finishes: a caller that waits forever turns one
    wedged cluster call into a permanently occupied worker."""
    import time

    from cloudlet_apis.errors import ServiceUnavailableError

    d = Deployer(settings_with_regions())
    d._read_timeout = 0.05

    start = time.monotonic()
    with pytest.raises(ServiceUnavailableError):
        await d.run_read(lambda: time.sleep(0.5))
    assert time.monotonic() - start < 0.4


async def test_an_admission_is_given_back_when_the_pool_refuses_the_submit():
    """A read that never reaches a thread gets no done callback either, so the
    slot it took would be held for the life of the process."""
    from api.services.regions.deployer import ReadPool

    pool = ReadPool(workers=1, max_queued=0)
    pool.shutdown()

    with pytest.raises(RuntimeError):
        await pool.run(lambda: "never runs")
    assert pool._inflight == 0


def test_deployer_close_releases_every_cluster():
    closed = []

    class _C:
        def __init__(self, name):
            self.name = name
            self.region = name

        def close(self):
            closed.append(self.region)

    d = Deployer(settings_with_regions())
    d._clusters = {"a": _C("a"), "b": _C("b")}
    d.close()
    assert sorted(closed) == ["a", "b"]


def test_cluster_close_closes_api_client_and_resets():
    from common.cluster import Cluster

    settings = settings_with_regions()
    c = Cluster(settings.regions[0], settings)
    calls = {"n": 0}

    class _Api:
        def close(self):
            calls["n"] += 1

    c._api_client_obj = _Api()
    c._dynamic_client_obj = object()
    c.close()
    assert calls["n"] == 1
    assert c._api_client_obj is None and c._dynamic_client_obj is None
    c.close()  # idempotent: no client to close now
    assert calls["n"] == 1


def test_overall_status_terminating_precedence():
    from api.services.regions.rollup import overall_status

    assert overall_status(["Terminating", "Ready"]) == "Terminating"
    assert overall_status(["Terminating", "Deploying"]) == "Terminating"
    # A real failure still outranks a termination in progress.
    assert overall_status(["Failed", "Terminating"]) == "Failed"


async def test_admission_counts_the_thread_not_the_await():
    """A read the caller timed out on is still occupying its worker; the pool
    must refuse new work while zombies hold it, or shedding never fires."""
    import threading

    from cloudlet_apis.errors import ServiceUnavailableError

    from api.services.regions.deployer import ReadPool

    pool = ReadPool(workers=1, max_queued=0)
    release = threading.Event()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.run(release.wait), timeout=0.05)
        # The await is gone; the THREAD is not. The slot must still be held.
        with pytest.raises(ServiceUnavailableError):
            await pool.run(lambda: "must be refused")
        release.set()
        await asyncio.sleep(0.1)  # let the zombie finish and release via its callback
        assert await pool.run(lambda: "ok") == "ok"
    finally:
        release.set()
        pool.shutdown()
