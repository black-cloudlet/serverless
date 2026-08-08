"""The SSE streams: framing, the bounds, the follow loop, and the endpoints."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from cloudlet_apis.errors import NotFoundError, ServiceUnavailableError

from api.core.config import StreamConfig
from api.models.common import LogStreamOpen, StreamError, WorkloadStatsResponse
from api.services.streams import logs as logs_stream
from api.services.streams import sse
from api.services.streams import stats as stats_stream
from api.services.streams.capacity import StreamCapacity
from common.cluster import LogFollow

# Fast enough that a whole follow cycle fits in a test, and still exercising the
# same code paths the shipped defaults do.
FAST = StreamConfig(
    max_concurrent=2,
    max_pods=2,
    interval_seconds=0.05,
    min_interval_seconds=0.01,
    max_interval_seconds=1.0,
    heartbeat_seconds=0.05,
    queue_size=5,
    max_seconds=3600,
)


@pytest.fixture
def capacity():
    cap = StreamCapacity(FAST)
    yield cap
    cap.shutdown()


async def collect(stream, count, timeout=5.0):
    """Take ``count`` events off a stream, then close it."""
    got = []

    async def drain():
        async for event in stream:
            got.append(event)
            if len(got) >= count:
                return

    try:
        await asyncio.wait_for(drain(), timeout)
    finally:
        await stream.aclose()
    return got


def names(events):
    return [e.name for e in events]


# --- framing ---------------------------------------------------------------


def test_an_event_renders_as_a_named_sse_frame():
    frame = sse.render(sse.StreamEvent("error", StreamError(code="NOT_FOUND", message="gone")))
    assert frame.startswith("event: error\ndata: {")
    assert frame.endswith("\n\n")  # the blank line is what ends an SSE event
    assert json.loads(frame.split("data: ", 1)[1].strip())["code"] == "NOT_FOUND"


def test_a_heartbeat_renders_as_a_comment_so_no_listener_fires():
    frame = sse.render(sse.heartbeat())
    assert frame.startswith(":")
    assert "event:" not in frame
    assert "data:" not in frame


def test_a_multiline_payload_stays_one_event():
    """A newline inside the data would otherwise truncate the frame."""

    class Multi(StreamError):
        def model_dump_json(self, **kwargs):
            return '{"a":\n"b"}'

    frame = sse.render(sse.StreamEvent("log", Multi(code="x", message="y")))
    assert frame == 'event: log\ndata: {"a":\ndata: "b"}\n\n'
    assert frame.count("\n\n") == 1


def test_the_preamble_sets_a_retry_and_flushes():
    assert sse.preamble().startswith(f"retry: {sse.RETRY_MS}\n")


# --- timestamps ------------------------------------------------------------


@pytest.mark.parametrize(
    "line,message",
    [
        ("2024-03-01T10:00:00.123456789Z hello world", "hello world"),
        ("2024-03-01T10:00:00.123Z hello", "hello"),
        ("2024-03-01T10:00:00Z hello", "hello"),
        ("2024-03-01T10:00:00+02:00 hello", "hello"),
    ],
)
def test_the_node_timestamp_is_split_off(line, message):
    stamp, text = logs_stream.split_timestamp(line)
    assert text == message
    assert stamp is not None
    # Rendered in Israel time like every other timestamp the API returns.
    assert stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() in (7200, 10800)


@pytest.mark.parametrize(
    "line",
    [
        "no timestamp here",
        "2024-13-45T99:99:99Z impossible",
        "2024-03-01 10:00:00 not rfc3339",
        "",
    ],
)
def test_a_line_without_a_parseable_stamp_keeps_all_of_itself(line):
    """Nothing may be eaten off the front of a line that had no timestamp."""
    stamp, text = logs_stream.split_timestamp(line)
    assert stamp is None
    assert text == line


def test_a_message_that_looks_like_a_timestamp_is_not_split_twice():
    stamp, text = logs_stream.split_timestamp("2024-03-01T10:00:00Z 2024-03-01T11:00:00Z later")
    assert stamp is not None
    assert text == "2024-03-01T11:00:00Z later"


# --- line assembly ---------------------------------------------------------


class _Chunks:
    """A urllib3-ish response that hands back whatever chunks it was given."""

    def __init__(self, chunks, block=None):
        self._chunks = chunks
        self._block = block
        self.closed = False

    def stream(self, amt=None, decode_content=True):
        for chunk in self._chunks:
            yield chunk
        if self._block is not None:
            self._block.wait()  # imitate a live follow: idle, not finished

    def close(self):
        self.closed = True
        if self._block is not None:
            self._block.set()

    def release_conn(self):
        pass


def test_lines_are_reassembled_across_chunk_boundaries():
    """The API server chunks wherever it likes; a line can span several."""
    follow = LogFollow(_Chunks([b"one\ntw", b"o\nthr", b"ee\n"]))
    assert list(follow.lines()) == ["one", "two", "three"]


def test_several_lines_in_one_chunk_are_split():
    follow = LogFollow(_Chunks([b"a\nb\nc\n"]))
    assert list(follow.lines()) == ["a", "b", "c"]


def test_a_trailing_line_without_a_newline_is_not_swallowed():
    follow = LogFollow(_Chunks([b"finished writing"]))
    assert list(follow.lines()) == ["finished writing"]


def test_only_newline_splits_lines():
    r"""A workload logging \v or \f logs one line, not three."""
    follow = LogFollow(_Chunks([b"a\x0bb\x0cc\n"]))
    assert list(follow.lines()) == ["a\x0bb\x0cc"]


def test_carriage_returns_are_stripped():
    follow = LogFollow(_Chunks([b"windows\r\nlines\r\n"]))
    assert list(follow.lines()) == ["windows", "lines"]


def test_undecodable_bytes_do_not_kill_the_stream():
    follow = LogFollow(_Chunks([b"good\n\xff\xfe bad\n"]))
    assert list(follow.lines())[0] == "good"


def test_close_is_idempotent_and_survives_a_response_that_raises():
    class Angry:
        def close(self):
            raise OSError("already gone")

        def release_conn(self):
            raise OSError("already gone")

    follow = LogFollow(Angry())
    follow.close()
    follow.close()  # teardown must never be the thing that fails


# --- admission -------------------------------------------------------------


def test_the_pool_is_sized_for_every_stream_it_will_admit():
    """A pool smaller than the admissions it serves turns a bound into a stall."""
    assert FAST.max_workers >= FAST.max_concurrent * FAST.max_pods


def test_slots_are_handed_back_when_a_stream_ends(capacity):
    with capacity.slot():
        assert capacity.open_streams == 1
    assert capacity.open_streams == 0


def test_a_slot_is_handed_back_even_when_the_stream_raises(capacity):
    with pytest.raises(RuntimeError), capacity.slot():
        raise RuntimeError("boom")
    assert capacity.open_streams == 0


def test_the_stream_past_the_limit_is_refused_not_queued(capacity):
    held = [capacity.slot() for _ in range(FAST.max_concurrent)]
    for slot in held:
        slot.__enter__()
    try:
        with pytest.raises(ServiceUnavailableError) as caught:
            with capacity.slot():
                pass
        assert "too many open streams" in caught.value.message
        assert caught.value.status_code == 503
    finally:
        for slot in held:
            slot.__exit__(None, None, None)
    # ...and the limit is not permanent: the next caller gets in.
    with capacity.slot():
        assert capacity.open_streams == 1


@pytest.mark.parametrize(
    "requested,expected",
    [(None, 0.05), (0.001, 0.01), (999.0, 1.0), (0.5, 0.5)],
)
def test_a_requested_interval_is_clamped_to_the_configured_bounds(capacity, requested, expected):
    assert capacity.interval(requested) == expected


def test_the_interval_bounds_have_to_be_coherent():
    with pytest.raises(ValueError):
        StreamConfig(min_interval_seconds=10, max_interval_seconds=1)
    with pytest.raises(ValueError):
        StreamConfig(interval_seconds=99, min_interval_seconds=1, max_interval_seconds=10)


# --- the buffer between the threads and the loop ---------------------------


async def test_the_buffer_drops_and_counts_rather_than_growing():
    """A workload outrunning its reader must cost a reported gap, not memory."""
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=3)
    for i in range(10):
        buf.put(sse.StreamEvent("log", StreamError(code="c", message=str(i))))

    drained = buf.drain()
    assert len(drained) == 3
    assert buf.take_dropped() == 7
    assert buf.take_dropped() == 0  # taking resets, so it is not double-reported
    # The oldest are kept, so what a client sees is a prefix, not a shuffle.
    assert [e.data.message for e in drained] == ["0", "1", "2"]


async def test_the_buffer_wakes_the_loop_from_a_thread():
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=10)

    def producer():
        time.sleep(0.05)
        buf.put(sse.StreamEvent("log", StreamError(code="c", message="from a thread")))

    threading.Thread(target=producer, daemon=True).start()
    await buf.wait(2.0)
    assert [e.data.message for e in buf.drain()] == ["from a thread"]


async def test_a_wakeup_is_not_lost_when_a_put_races_a_drain():
    """The edge-triggered wakeup is only safe because drain empties under the lock."""
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=10)
    buf.put(sse.StreamEvent("log", StreamError(code="c", message="first")))
    assert len(buf.drain()) == 1
    # Deque is empty again, so this put must wake the loop rather than assume
    # the previous (already consumed) wakeup still stands.
    buf.put(sse.StreamEvent("log", StreamError(code="c", message="second")))
    await asyncio.wait_for(buf.wait(1.0), 2.0)
    assert len(buf.drain()) == 1


# --- following logs --------------------------------------------------------


class FakeCluster:
    """A local site with a fixed pod list and scripted per-pod log chunks."""

    site = "central"

    def __init__(self, pods=None, chunks=None, *, live=False):
        self._pods = dict(pods or {})
        self._chunks = chunks or {}
        self._live = live
        self.followed: list[str] = []
        self.follows: list[_Chunks] = []
        self.blocks: list[threading.Event] = []
        self.list_calls = 0
        self._lock = threading.Lock()

    def set_pods(self, pods):
        with self._lock:
            self._pods = dict(pods)

    def get(self, kind, name=None, label_selector=None):
        self.list_calls += 1
        with self._lock:
            return [
                {
                    "metadata": {
                        "name": pod,
                        "labels": {logs_stream.REVISION_LABEL: revision},
                    }
                }
                for pod, revision in self._pods.items()
            ]

    def follow_pod_logs(self, pod, *, container, since_seconds=None):
        self.followed.append(pod)
        block = threading.Event() if self._live else None
        if block is not None:
            self.blocks.append(block)
        response = _Chunks(self._chunks.get(pod, []), block=block)
        self.follows.append(response)
        return LogFollow(response)


def _opening(container="user-container", pods=()):
    return LogStreamOpen(
        name="foo",
        group="team",
        type="function",
        site="central",
        container=container,
        pods=sorted(pods),
    )


def _follow(cluster, capacity, pods, **over):
    args = dict(
        cluster=cluster,
        capacity=capacity,
        config=FAST,
        opening=_opening(pods=pods),
        oname="foo-team",
        pods=pods,
        since_seconds=None,
        interval=FAST.interval_seconds,
    )
    args.update(over)
    return logs_stream.follow(**args)


async def test_the_stream_opens_by_saying_what_it_is_following(capacity):
    cluster = FakeCluster({"p1": "foo-team-00001"}, {"p1": [b"hello\n"]})
    events = await collect(_follow(cluster, capacity, {"p1": "foo-team-00001"}), 2)

    assert events[0].name == "open"
    assert events[0].data.pods == ["p1"]
    assert events[0].data.site == "central"


async def test_log_lines_arrive_as_events_carrying_their_pod_and_revision(capacity):
    cluster = FakeCluster(
        {"p1": "foo-team-00007"},
        {"p1": [b"2024-03-01T10:00:00Z first\n2024-03-01T10:00:01Z second\n"]},
    )
    events = await collect(_follow(cluster, capacity, {"p1": "foo-team-00007"}), 3)

    logs = [e for e in events if e.name == "log"]
    assert [line.data.message for line in logs] == ["first", "second"]
    assert logs[0].data.pod == "p1"
    assert logs[0].data.revision == "foo-team-00007"
    assert logs[0].data.container == "user-container"
    assert logs[0].data.time is not None


async def test_lines_from_several_pods_are_interleaved_into_one_stream(capacity):
    cluster = FakeCluster({"p1": "r1", "p2": "r1"}, {"p1": [b"from-one\n"], "p2": [b"from-two\n"]})
    events = await collect(_follow(cluster, capacity, {"p1": "r1", "p2": "r1"}), 3)

    assert {e.data.pod for e in events if e.name == "log"} == {"p1", "p2"}


async def test_a_scaled_to_zero_workload_opens_and_waits_instead_of_failing(capacity):
    """Empty pods is a normal state, not an error - the stream stays open."""
    cluster = FakeCluster({})
    events = await collect(_follow(cluster, capacity, {}), 2)

    assert events[0].name == "open"
    assert events[0].data.pods == []
    assert events[1].name == "heartbeat"  # nothing to say, connection kept alive


async def test_a_pod_appearing_later_starts_being_followed(capacity):
    """A scale-up mid-stream must not need the client to reconnect."""
    cluster = FakeCluster({}, {"p-new": [b"i just started\n"]})
    stream = _follow(cluster, capacity, {})
    got = []

    async def run():
        async for event in stream:
            got.append(event)
            if event.name == "open":
                cluster.set_pods({"p-new": "r1"})
            if event.name == "log":
                return

    try:
        await asyncio.wait_for(run(), 5.0)
    finally:
        await stream.aclose()

    assert "pods" in names(got)
    change = next(e for e in got if e.name == "pods")
    assert change.data.added == ["p-new"]
    assert change.data.following == ["p-new"]
    assert got[-1].data.message == "i just started"


async def test_a_pod_going_away_is_reported_and_its_follow_ended(capacity):
    cluster = FakeCluster({"p1": "r1"}, live=True)
    stream = _follow(cluster, capacity, {"p1": "r1"})
    got = []

    async def run():
        async for event in stream:
            got.append(event)
            if event.name == "open":
                cluster.set_pods({})
            if event.name == "pods":
                return

    try:
        await asyncio.wait_for(run(), 5.0)
    finally:
        await stream.aclose()

    change = next(e for e in got if e.name == "pods")
    assert change.data.removed == ["p1"]
    assert change.data.following == []
    assert cluster.follows[0].closed  # the thread was actually released


async def test_following_more_pods_than_the_cap_warns_and_names_them(capacity):
    """Exceeding the cap must be visible; silence reads as "logged nothing"."""
    pods = {f"p{i}": "r1" for i in range(5)}
    cluster = FakeCluster(pods, live=True)
    events = await collect(_follow(cluster, capacity, pods), 2)

    warning = next(e for e in events if e.name == "warning")
    assert len(warning.data.pods) == 5 - FAST.max_pods
    assert len(cluster.followed) == FAST.max_pods


async def test_a_pod_that_cannot_be_followed_warns_without_ending_the_stream(capacity):
    class Refusing(FakeCluster):
        def follow_pod_logs(self, pod, *, container, since_seconds=None):
            if pod == "bad":
                raise RuntimeError("forbidden")
            return super().follow_pod_logs(pod, container=container, since_seconds=since_seconds)

    cluster = Refusing({"bad": "r1", "good": "r1"}, {"good": [b"still here\n"]})
    events = await collect(_follow(cluster, capacity, {"bad": "r1", "good": "r1"}), 3)

    assert "warning" in names(events)
    assert any(e.name == "log" and e.data.message == "still here" for e in events)


async def test_a_pod_that_vanished_between_listing_and_reading_is_not_an_error(capacity):
    class Raced(FakeCluster):
        def follow_pod_logs(self, pod, *, container, since_seconds=None):
            raise NotFoundError("pod 'gone' not found")

    cluster = Raced({"gone": "r1"})
    events = await collect(_follow(cluster, capacity, {"gone": "r1"}), 2)

    assert names(events) == ["open", "heartbeat"]  # no warning; this is normal churn


async def test_a_failed_re_list_does_not_end_the_stream(capacity):
    class Flaky(FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            self.list_calls += 1
            if self.list_calls > 0:
                raise RuntimeError("api server blipped")
            return []

    cluster = Flaky({})
    events = await collect(_follow(cluster, capacity, {}), 3)

    assert names(events) == ["open", "heartbeat", "heartbeat"]


async def test_teardown_closes_every_follow(capacity):
    """The client going away must return the threads, not leak them."""
    pods = {"p1": "r1", "p2": "r1"}
    cluster = FakeCluster(pods, live=True)
    await collect(_follow(cluster, capacity, pods), 1)

    assert len(cluster.follows) == 2
    assert all(f.closed for f in cluster.follows)


async def test_the_slow_client_warning_reports_how_much_was_lost(capacity):
    cluster = FakeCluster({"p1": "r1"}, {"p1": [b"x\n" * 50]})
    events = await collect(_follow(cluster, capacity, {"p1": "r1"}), 12, timeout=5.0)

    warnings = [e for e in events if e.name == "warning"]
    assert warnings, "a queue overflow has to be reported"
    assert warnings[0].data.droppedLines > 0


# --- streaming stats -------------------------------------------------------


def _stats(replicas):
    return WorkloadStatsResponse(overallStatus="Ready", replicas=replicas, sites=[])


async def test_the_first_reading_is_sent_immediately():
    """A client must not stare at an empty panel for a whole interval."""

    async def read():
        raise AssertionError("must not be called before the first interval")

    stream = stats_stream.follow(config=FAST, first=_stats(1), read=read, interval=10.0)
    events = await collect(stream, 1)

    assert events[0].name == "stats"
    assert events[0].data.replicas == 1


async def test_readings_keep_arriving_on_the_interval():
    counter = iter(range(2, 100))

    async def read():
        return _stats(next(counter))

    stream = stats_stream.follow(config=FAST, first=_stats(1), read=read, interval=0.02)
    events = await collect(stream, 3)

    assert [e.data.replicas for e in events if e.name == "stats"] == [1, 2, 3]


async def test_a_deleted_workload_ends_the_stream_with_the_envelope_s_own_code():
    async def read():
        raise NotFoundError("function 'foo' not found")

    stream = stats_stream.follow(config=FAST, first=_stats(1), read=read, interval=0.01)
    events = [e async for e in stream]

    assert names(events) == ["stats", "error"]
    assert events[-1].data.code == "NOT_FOUND"
    assert "not found" in events[-1].data.message


async def test_an_unexpected_failure_ends_the_stream_without_leaking_its_text():
    async def read():
        raise RuntimeError("postgres://user:hunter2@db.internal/secrets")

    stream = stats_stream.follow(config=FAST, first=_stats(1), read=read, interval=0.01)
    events = [e async for e in stream]

    assert events[-1].name == "error"
    assert events[-1].data.code == "INTERNAL"
    assert events[-1].data.message == "Internal server error."
    assert "hunter2" not in events[-1].data.message


async def test_a_long_interval_still_sends_heartbeats():
    """A minute of silence is what an idle-timeout somewhere in the path kills."""

    async def read():
        return _stats(2)

    config = FAST.model_copy(update={"heartbeat_seconds": 0.02})
    stream = stats_stream.follow(config=config, first=_stats(1), read=read, interval=1.0)
    events = await collect(stream, 3)

    assert names(events) == ["stats", "heartbeat", "heartbeat"]


async def test_a_stream_ends_itself_at_the_lifetime_cap():
    """An immortal stream is how a leaked client survives forever."""

    async def read():
        return _stats(2)

    config = FAST.model_copy(update={"max_seconds": 1, "heartbeat_seconds": 0.02})
    stream = stats_stream.follow(config=config, first=_stats(1), read=read, interval=0.05)

    events = []
    await asyncio.wait_for(asyncio.create_task(_drain_all(stream, events)), 10.0)
    assert events[0].name == "stats"
    assert len(events) > 1  # it ran, then stopped on its own


async def _drain_all(stream, into):
    async for event in stream:
        into.append(event)


# --- the engine's orchestration --------------------------------------------
#
# What the two service methods own is the order of operations: take a slot,
# authorize, and only then start streaming - so that everything with a status
# code happens while an error envelope is still possible, and the slot comes
# back whether or not it did.


def _engine(cluster, capacity, *, owner_group="team", offering="function"):
    from api.core.config import Settings, SiteConfig
    from api.models.common import LABEL_GROUP, LABEL_OFFERING
    from api.services.sites.deployer import Deployer
    from api.services.workloads import WorkloadService

    settings = Settings(sites=[SiteConfig(name="central", cluster="central-0")], auth_enabled=False)
    deployer = Deployer(settings)
    deployer._clusters = {"central": cluster}
    deployer._local_site = "central"
    cluster.ksvc = {
        "metadata": {
            "name": "foo-team",
            "labels": {LABEL_GROUP: owner_group, LABEL_OFFERING: offering},
        }
    }

    class _NoBuild:
        has_build = False

        def plan(self, *a, **k):
            raise AssertionError("not used")

    return WorkloadService(settings, deployer, _NoBuild(), capacity)


class OwnedCluster(FakeCluster):
    """A FakeCluster that also answers the KSVC read the authorization does."""

    ksvc: dict | None = None

    def get(self, kind, name=None, label_selector=None):
        from common.cluster import ResourceKind

        if kind is ResourceKind.KNATIVE_SERVICE:
            if self.ksvc is None:
                raise NotFoundError("function 'foo' not found")
            return self.ksvc
        return super().get(kind, name, label_selector)


def _caller(groups=("team",)):
    from cloudlet_apis.auth import Principal

    return Principal(subject="u", username="alice", groups=list(groups))


async def test_stream_logs_authorizes_before_the_first_event(capacity):
    cluster = OwnedCluster({"p1": "r1"}, {"p1": [b"hello\n"]})
    engine = _engine(cluster, capacity)

    stream = await engine.stream_logs(
        _offering(), "foo", _caller(), "team", container="user-container", since_seconds=None
    )
    try:
        events = await collect(stream, 2)
    finally:
        pass

    assert events[0].name == "open"
    assert events[0].data.name == "foo"
    assert events[0].data.site == "central"


async def test_stream_logs_404s_a_workload_that_is_not_here_and_keeps_the_slot(capacity):
    """A 404 must be an envelope, and must not cost a slot that never opened."""
    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity)
    engine.deployer._clusters["central"].ksvc = None

    with pytest.raises(NotFoundError):
        await engine.stream_logs(
            _offering(), "foo", _caller(), "team", container="user-container", since_seconds=None
        )
    assert capacity.open_streams == 0


async def test_stream_logs_hides_another_group_s_workload_as_a_404(capacity):
    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity, owner_group="someone-else")

    with pytest.raises(NotFoundError):
        await engine.stream_logs(
            _offering(), "foo", _caller(), "team", container="user-container", since_seconds=None
        )
    assert capacity.open_streams == 0


async def test_a_caller_outside_the_group_is_refused_before_any_cluster_read(capacity):
    from cloudlet_apis.errors import ForbiddenError

    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity)

    with pytest.raises(ForbiddenError):
        await engine.stream_logs(
            _offering(),
            "foo",
            _caller(groups=["other"]),
            "team",
            container="user-container",
            since_seconds=None,
        )
    assert cluster.list_calls == 0
    assert capacity.open_streams == 0


async def test_the_slot_is_held_for_the_stream_and_returned_when_it_ends(capacity):
    cluster = OwnedCluster({"p1": "r1"}, live=True)
    engine = _engine(cluster, capacity)

    stream = await engine.stream_logs(
        _offering(), "foo", _caller(), "team", container="user-container", since_seconds=None
    )
    assert capacity.open_streams == 1  # taken at open, not at the first event
    await collect(stream, 1)
    assert capacity.open_streams == 0


async def test_streams_past_the_limit_are_refused(capacity):
    cluster = OwnedCluster({}, live=True)
    engine = _engine(cluster, capacity)
    opened = []
    for _ in range(FAST.max_concurrent):
        opened.append(
            await engine.stream_logs(
                _offering(),
                "foo",
                _caller(),
                "team",
                container="user-container",
                since_seconds=None,
            )
        )
    try:
        with pytest.raises(ServiceUnavailableError):
            await engine.stream_logs(
                _offering(),
                "foo",
                _caller(),
                "team",
                container="user-container",
                since_seconds=None,
            )
    finally:
        for stream in opened:
            await stream.aclose()
    assert capacity.open_streams == 0


async def test_stream_stats_takes_the_first_reading_before_opening(capacity):
    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity)
    readings = []

    async def fake_stats(offering, name, user, group, *, executor=None):
        readings.append(executor)
        return _stats(3)

    engine.stats = fake_stats
    stream = await engine.stream_stats(_offering(), "foo", _caller(), "team")
    try:
        events = await collect(stream, 1)
    finally:
        await stream.aclose()

    assert events[0].name == "stats"
    assert events[0].data.replicas == 3
    # ...on the stream pool, not the default one every request shares.
    assert readings[0] is capacity.executor


async def test_stream_stats_returns_the_slot_when_the_first_reading_fails(capacity):
    engine = _engine(OwnedCluster({}), capacity)

    async def fake_stats(*a, **k):
        raise NotFoundError("function 'foo' not found")

    engine.stats = fake_stats
    with pytest.raises(NotFoundError):
        await engine.stream_stats(_offering(), "foo", _caller(), "team")
    assert capacity.open_streams == 0


def _offering():
    from api.services.offering import FUNCTION

    return FUNCTION
