"""The SSE streams: framing, the bounds, the follow loop, and the endpoints."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from cloudlet_apis.errors import NotFoundError, ServiceUnavailableError

from api.core.config import StreamConfig
from api.models.common import (
    PodLogStreamOpen,
    PodRoster,
    StreamError,
    WorkloadStatsResponse,
)
from api.services.streams import logs as logs_stream
from api.services.streams import pods as pods_stream
from api.services.streams import sse
from api.services.streams import stats as stats_stream
from api.services.streams.capacity import StreamCapacity
from common.cluster import LogFollow

# Fast enough that a whole follow cycle fits in a test, and still exercising the
# same code paths the shipped defaults do.
FAST = StreamConfig(
    max_concurrent=2,
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
    # A str item is a pre-rendered batch of `log` frames - the hot path is
    # rendered on the follower thread so the event loop only forwards bytes.
    return [("log" if isinstance(e, str) else e.name) for e in events]


def log_lines(events):
    """The JSON payloads of the pre-rendered ``log`` frames, in order."""
    payloads = []
    for e in events:
        if not isinstance(e, str):
            continue
        assert e.startswith("event: log\n"), "only log frames cross the stream pre-rendered"
        for line in e.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[len("data: ") :]))
    return payloads


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


def test_a_drain_of_rendered_frames_coalesces_into_one_yield():
    """The loop pays one yield per burst, not per line - the fix for a chatty
    pod starving the health probes off the shared event loop."""
    beat = sse.heartbeat()
    items = ["event: log\ndata: 1\n\n", "event: log\ndata: 2\n\n", beat, "event: log\ndata: 3\n\n"]

    out = list(logs_stream._coalesced(items))

    assert out == [
        "event: log\ndata: 1\n\nevent: log\ndata: 2\n\n",  # wire-identical: frames concatenate
        beat,
        "event: log\ndata: 3\n\n",
    ]


async def test_the_response_passes_pre_rendered_frames_through_untouched():
    from api.routers import sse as sse_router

    async def gen():
        yield "event: log\ndata: {}\n\n"
        yield sse.heartbeat()

    resp = sse_router.stream(gen())
    chunks = [c async for c in resp.body_iterator]

    assert chunks[0] == sse.preamble()
    assert chunks[1] == "event: log\ndata: {}\n\n"
    assert chunks[2] == ": heartbeat\n\n"


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


def test_a_neverending_line_is_emitted_rather_than_held():
    """A container spewing bytes with no newline must cost delivery in pieces,
    not the process's memory - the reassembly buffer is the API's, and growing
    it until the pod is OOM-killed is a crash wearing a log line's clothes."""
    follow = LogFollow(_Chunks([b"x" * 10, b"y" * 10, b"z\ntail\n"]))
    follow.MAX_LINE_BYTES = 8
    pieces = list(follow.lines())
    assert pieces[-1] == "tail"
    assert "".join(pieces[:-1]) == "x" * 10 + "y" * 10 + "z"
    assert all(len(p) <= 16 for p in pieces)  # one chunk can at most double it


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
    """A pool smaller than the admissions it serves turns a bound into a stall.

    Every admitted stream may hold one thread for its whole life (a log follow),
    and a pods or stats stream needs one more, briefly, on each tick.
    """
    assert FAST.max_workers >= FAST.max_concurrent * 2


def test_slots_are_handed_back_when_a_stream_ends(capacity):
    slot = capacity.admit()
    assert capacity.open_streams == 1
    slot.release()
    assert capacity.open_streams == 0


def test_a_double_release_returns_the_slot_once(capacity):
    """Several owners may release one slot (finally, error path, GC backstop);
    only the first may count, or the gate over-admits forever after."""
    slot = capacity.admit()
    slot.release()
    slot.release()
    assert capacity.open_streams == 0
    # And the count cannot go negative: the next admit/release pair is exact.
    other = capacity.admit()
    assert capacity.open_streams == 1
    other.release()
    assert capacity.open_streams == 0


def test_the_stream_past_the_limit_is_refused_not_queued(capacity):
    held = [capacity.admit() for _ in range(FAST.max_concurrent)]
    try:
        with pytest.raises(ServiceUnavailableError) as caught:
            capacity.admit()
        assert "too many open streams" in caught.value.message
        assert caught.value.status_code == 503
    finally:
        for slot in held:
            slot.release()
    # ...and the limit is not permanent: the next caller gets in.
    slot = capacity.admit()
    assert capacity.open_streams == 1
    slot.release()


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
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=3, max_bytes=1 << 20)
    for i in range(10):
        buf.put(sse.StreamEvent("log", StreamError(code="c", message=str(i))))

    drained = buf.drain()
    assert len(drained) == 3
    assert buf.take_dropped() == 7
    assert buf.take_dropped() == 0  # taking resets, so it is not double-reported
    # The oldest are kept, so what a client sees is a prefix, not a shuffle.
    assert [e.data.message for e in drained] == ["0", "1", "2"]


async def test_the_buffer_wakes_the_loop_from_a_thread():
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=10, max_bytes=1 << 20)

    def producer():
        time.sleep(0.05)
        buf.put(sse.StreamEvent("log", StreamError(code="c", message="from a thread")))

    threading.Thread(target=producer, daemon=True).start()
    await buf.wait(2.0)
    assert [e.data.message for e in buf.drain()] == ["from a thread"]


async def test_a_wakeup_is_not_lost_when_a_put_races_a_drain():
    """The edge-triggered wakeup is only safe because drain empties under the lock."""
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=10, max_bytes=1 << 20)
    buf.put(sse.StreamEvent("log", StreamError(code="c", message="first")))
    assert len(buf.drain()) == 1
    # Deque is empty again, so this put must wake the loop rather than assume
    # the previous (already consumed) wakeup still stands.
    buf.put(sse.StreamEvent("log", StreamError(code="c", message="second")))
    await asyncio.wait_for(buf.wait(1.0), 2.0)
    assert len(buf.drain()) == 1


# --- following one pod's log ------------------------------------------------


class FakeCluster:
    """A local region: a scripted pod roster, and scripted per-pod log chunks."""

    region = "central"
    oname = "foo-team"

    def __init__(self, pods=None, chunks=None, *, live=False):
        self._pods = dict(pods or {})
        self._chunks = chunks or {}
        self._live = live
        self.followed: list[str] = []
        self.follow_bounds: list[tuple] = []
        self.follows: list[_Chunks] = []
        self.list_calls = 0
        self._lock = threading.Lock()

    def set_pods(self, pods):
        with self._lock:
            self._pods = dict(pods)

    def _pod_obj(self, name, revision):
        return {
            "metadata": {
                "name": name,
                "labels": {
                    pods_stream.REVISION_LABEL: revision,
                    # What the per-pod authorization checks: a pod is only this
                    # workload's if it carries this workload's service label.
                    pods_stream.SERVICE_LABEL: self.oname,
                },
            },
            "status": {
                "phase": "Running",
                "startTime": "2024-03-01T10:00:00Z",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"restartCount": 1}],
            },
        }

    def get(self, kind, name=None, label_selector=None):
        from common.cluster import ResourceKind

        if kind is ResourceKind.POD_METRICS:
            return [
                {
                    "metadata": {"name": pod},
                    "containers": [
                        {"name": "user-container", "usage": {"cpu": "100m", "memory": "64Mi"}},
                        {"name": "queue-proxy", "usage": {"cpu": "900m", "memory": "900Mi"}},
                    ],
                }
                for pod in self._pods
            ]
        if name is not None:  # a get of one pod by name
            with self._lock:
                if name not in self._pods:
                    raise NotFoundError(f"pod '{name}' not found")
                return self._pod_obj(name, self._pods[name])
        self.list_calls += 1
        with self._lock:
            return [self._pod_obj(pod, rev) for pod, rev in self._pods.items()]

    def follow_pod_logs(self, pod, *, container, since_seconds=None, tail_lines=None):
        self.followed.append(pod)
        self.follow_bounds.append((since_seconds, tail_lines))
        block = threading.Event() if self._live else None
        response = _Chunks(self._chunks.get(pod, []), block=block)
        self.follows.append(response)
        return LogFollow(response)


def _opening(pod="p1", container="user-container", revision="r1"):
    return PodLogStreamOpen(
        name="foo",
        group="team",
        type="function",
        region="central",
        pod=pod,
        container=container,
        revision=revision,
    )


def _follow(cluster, capacity, **over):
    args = dict(
        cluster=cluster,
        capacity=capacity,
        config=FAST,
        opening=_opening(),
        since_seconds=None,
    )
    args.update(over)
    return logs_stream.follow(**args)


async def test_the_stream_opens_by_saying_which_pod_it_is_following(capacity):
    cluster = FakeCluster({"p1": "r1"}, {"p1": [b"hello\n"]})
    events = await collect(_follow(cluster, capacity), 2)

    assert events[0].name == "open"
    assert events[0].data.pod == "p1"
    assert events[0].data.region == "central"
    assert cluster.followed == ["p1"]


async def test_log_lines_arrive_as_rendered_frames_carrying_their_pod_and_revision(capacity):
    """Lines cross pre-rendered (str), so the loop only forwards bytes."""
    cluster = FakeCluster(
        {"p1": "foo-team-00007"},
        {"p1": [b"2024-03-01T10:00:00Z first\n2024-03-01T10:00:01Z second\n"]},
    )
    events = [
        e async for e in _follow(cluster, capacity, opening=_opening(revision="foo-team-00007"))
    ]

    lines = log_lines(events)
    assert [line["message"] for line in lines] == ["first", "second"]
    assert lines[0]["pod"] == "p1"
    assert lines[0]["revision"] == "foo-team-00007"
    assert lines[0]["container"] == "user-container"
    assert lines[0]["time"] is not None


async def test_only_the_named_pod_is_followed(capacity):
    """The whole point of the per-pod shape: one stream, one pod, one thread."""
    cluster = FakeCluster({"p1": "r1", "p2": "r1"}, {"p1": [b"mine\n"], "p2": [b"not mine\n"]})
    events = [e async for e in _follow(cluster, capacity)]

    assert cluster.followed == ["p1"]
    assert [line["message"] for line in log_lines(events)] == ["mine"]


async def test_the_stream_ends_when_the_pod_s_log_does(capacity):
    """A scale-down or a new revision - routine on Knative, so not an error."""
    cluster = FakeCluster({"p1": "r1"}, {"p1": [b"last words\n"]})
    events = [e async for e in _follow(cluster, capacity)]

    assert names(events)[0] == "open"
    assert names(events)[-1] == "end"
    assert "replaced" in events[-1].data.reason
    # ...and the final lines are delivered before it closes.
    assert [line["message"] for line in log_lines(events)] == ["last words"]


async def test_a_pod_that_cannot_be_read_warns_and_ends_rather_than_hanging(capacity):
    class Refusing(FakeCluster):
        def follow_pod_logs(self, pod, *, container, since_seconds=None, tail_lines=None):
            raise RuntimeError("forbidden")

    events = [e async for e in _follow(Refusing({"p1": "r1"}), capacity)]

    assert "warning" in names(events)
    assert names(events)[-1] == "end"


async def test_teardown_closes_the_follow(capacity):
    """The client going away must return the thread, not leak it."""
    cluster = FakeCluster({"p1": "r1"}, live=True)
    await collect(_follow(cluster, capacity), 1)

    assert len(cluster.follows) == 1
    assert cluster.follows[0].closed


async def test_the_slow_client_warning_reports_how_much_was_lost(capacity):
    cluster = FakeCluster({"p1": "r1"}, {"p1": [b"x\n" * 50]})
    events = [e async for e in _follow(cluster, capacity)]

    warnings = [e for e in events if not isinstance(e, str) and e.name == "warning"]
    assert warnings, "a queue overflow has to be reported"
    assert warnings[0].data.droppedLines > 0


async def test_a_quiet_pod_is_heartbeated_not_closed(capacity):
    cluster = FakeCluster({"p1": "r1"}, live=True)
    events = await collect(_follow(cluster, capacity), 3)

    assert names(events) == ["open", "heartbeat", "heartbeat"]


async def test_a_log_stream_ends_itself_at_the_lifetime_cap(capacity):
    """An immortal stream is how a leaked client survives forever."""
    cluster = FakeCluster({"p1": "r1"}, live=True)
    config = FAST.model_copy(update={"max_seconds": 1, "heartbeat_seconds": 0.02})
    events = [e async for e in _follow(cluster, capacity, config=config)]

    assert names(events)[-1] == "end"
    assert "time limit" in events[-1].data.reason


# --- the pod roster ---------------------------------------------------------


def test_the_roster_reports_what_a_client_needs_to_pick_a_pod():
    cluster = FakeCluster({"p2": "r1", "p1": "r1"})
    roster = pods_stream.read_roster(cluster, "foo-team")

    assert [p.pod for p in roster] == ["p1", "p2"]  # ordered, not listing order
    assert roster[0].revision == "r1"
    assert roster[0].phase == "Running"
    assert roster[0].ready is True
    assert roster[0].restarts == 1
    assert roster[0].startedAt is not None


def test_the_roster_excludes_the_sidecar_from_usage():
    """Same rule as /stats: the queue-proxy's usage is the platform's, not the user's."""
    roster = pods_stream.read_roster(FakeCluster({"p1": "r1"}), "foo-team")
    assert roster[0].usage.cpu == "100m"  # not 1000m
    assert roster[0].usage.memory == "64Mi"


def test_a_pod_with_no_metrics_yet_is_still_listed():
    """The newest pod is the one a client most wants to follow, and the one
    metrics-server has not scraped. Missing usage must never hide it."""

    class NoMetrics(FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            from common.cluster import ResourceKind

            if kind is ResourceKind.POD_METRICS:
                return []
            return super().get(kind, name, label_selector)

    roster = pods_stream.read_roster(NoMetrics({"p1": "r1"}), "foo-team")
    assert [p.pod for p in roster] == ["p1"]
    assert roster[0].usage is None


def test_an_unreadable_metrics_api_does_not_empty_the_roster():
    class BrokenMetrics(FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            from common.cluster import ResourceKind

            if kind is ResourceKind.POD_METRICS:
                raise RuntimeError("metrics-server is down")
            return super().get(kind, name, label_selector)

    roster = pods_stream.read_roster(BrokenMetrics({"p1": "r1"}), "foo-team")
    assert [p.pod for p in roster] == ["p1"]
    assert roster[0].usage is None


def test_a_pod_that_is_running_but_not_ready_says_so():
    class NotReady(FakeCluster):
        def _pod_obj(self, name, revision):
            obj = super()._pod_obj(name, revision)
            obj["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
            return obj

    roster = pods_stream.read_roster(NotReady({"p1": "r1"}), "foo-team")
    assert roster[0].phase == "Running"
    assert roster[0].ready is False


def _roster(pods=()):
    return PodRoster(name="foo", group="team", type="function", region="central", pods=list(pods))


def _pods_follow(cluster, capacity, **over):
    args = dict(
        cluster=cluster,
        capacity=capacity,
        config=FAST,
        first=_roster(),
        oname="foo-team",
        interval=FAST.interval_seconds,
    )
    args.update(over)
    return pods_stream.follow(**args)


async def test_the_first_roster_is_sent_immediately(capacity):
    """A client must be able to open a log stream without waiting an interval."""
    cluster = FakeCluster({"p1": "r1"})
    first = _roster(pods_stream.read_roster(cluster, "foo-team"))
    events = await collect(_pods_follow(cluster, capacity, first=first), 1)

    assert events[0].name == "pods"
    assert [p.pod for p in events[0].data.pods] == ["p1"]


async def test_the_roster_keeps_arriving_and_tracks_a_scale_up(capacity):
    cluster = FakeCluster({"p1": "r1"})
    stream = _pods_follow(cluster, capacity)
    got = []

    async def run():
        async for event in stream:
            got.append(event)
            if len(got) == 1:
                cluster.set_pods({"p1": "r1", "p2": "r1"})
            if len(got) >= 2:
                return

    try:
        await asyncio.wait_for(run(), 5.0)
    finally:
        await stream.aclose()

    assert [p.pod for p in got[-1].data.pods] == ["p1", "p2"]


async def test_a_scaled_to_zero_workload_streams_an_empty_roster(capacity):
    """Empty is a normal state, not an error - and the reason this is a stream."""
    events = await collect(_pods_follow(FakeCluster({}), capacity), 2)

    assert names(events) == ["pods", "pods"]
    assert events[0].data.pods == []


async def test_a_deleted_workload_ends_the_roster_stream_with_its_envelope_code(capacity):
    class Gone(FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            raise NotFoundError("function 'foo' not found")

    events = [e async for e in _pods_follow(Gone({}), capacity)]

    assert names(events) == ["pods", "error"]
    assert events[-1].data.code == "NOT_FOUND"


async def test_an_unexpected_roster_failure_does_not_leak_its_text(capacity):
    class Angry(FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            raise RuntimeError("postgres://user:hunter2@db.internal")

    events = [e async for e in _pods_follow(Angry({}), capacity)]

    assert events[-1].name == "error"
    assert events[-1].data.code == "INTERNAL"
    assert "hunter2" not in events[-1].data.message


# --- streaming stats -------------------------------------------------------


def _stats(replicas):
    return WorkloadStatsResponse(status="Ready", replicas=replicas, regions=[])


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
# What the service methods own is the order of operations: take a slot,
# authorize, and only then start streaming - so that everything with a status
# code happens while an error envelope is still possible, and the slot comes
# back whether or not it did.


def _engine(cluster, capacity, *, owner_group="team", offering="function"):
    from api.core.config import RegionConfig, Settings
    from api.models.common import LABEL_GROUP, LABEL_OFFERING
    from api.services.regions.deployer import Deployer
    from api.services.workloads import WorkloadService

    settings = Settings(
        regions=[RegionConfig(name="central", cluster="central-0")], auth_enabled=False
    )
    deployer = Deployer(settings)
    deployer._clusters = {"central": cluster}
    deployer._local_region = "central"
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


def _offering():
    from api.services.offering import FUNCTION

    return FUNCTION


# --- the pods stream --------------------------------------------------------


async def test_stream_pods_authorizes_and_sends_the_roster(capacity):
    engine = _engine(OwnedCluster({"p1": "r1"}), capacity)

    stream = await engine.stream_pods(_offering(), "foo", _caller(), "team")
    try:
        events = await collect(stream, 1)
    finally:
        await stream.aclose()

    assert events[0].name == "pods"
    assert events[0].data.name == "foo"
    assert events[0].data.region == "central"
    assert [p.pod for p in events[0].data.pods] == ["p1"]


async def test_stream_pods_404s_a_workload_that_is_not_here_and_keeps_the_slot(capacity):
    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity)
    cluster.ksvc = None

    with pytest.raises(NotFoundError):
        await engine.stream_pods(_offering(), "foo", _caller(), "team")
    assert capacity.open_streams == 0


async def test_stream_pods_hides_another_group_s_workload_as_a_404(capacity):
    engine = _engine(OwnedCluster({}), capacity, owner_group="someone-else")

    with pytest.raises(NotFoundError):
        await engine.stream_pods(_offering(), "foo", _caller(), "team")
    assert capacity.open_streams == 0


async def test_a_caller_outside_the_group_is_refused_before_any_cluster_read(capacity):
    from cloudlet_apis.errors import ForbiddenError

    cluster = OwnedCluster({})
    engine = _engine(cluster, capacity)

    with pytest.raises(ForbiddenError):
        await engine.stream_pods(_offering(), "foo", _caller(groups=["other"]), "team")
    assert cluster.list_calls == 0
    assert capacity.open_streams == 0


# --- the per-pod log stream -------------------------------------------------


async def test_stream_pod_logs_opens_on_the_named_pod(capacity):
    cluster = OwnedCluster({"p1": "rev-7"}, {"p1": [b"hi\n"]})
    engine = _engine(cluster, capacity)

    stream = await engine.stream_pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
    )
    try:
        events = await collect(stream, 2)
    finally:
        await stream.aclose()

    assert events[0].name == "open"
    assert events[0].data.pod == "p1"
    # The revision is read off the pod, not guessed from the workload.
    assert events[0].data.revision == "rev-7"


async def test_the_follow_opens_at_the_requested_tail_clamped_to_the_bound(capacity):
    """tailLines is what shows an *existing* pod's recent history - a pod quiet
    longer than any since-window would otherwise show nothing until it next
    writes - but a request for a million lines is still bounded server-side."""
    cluster = OwnedCluster({"p1": "rev-7"}, {"p1": [b"hi\n"]})
    engine = _engine(cluster, capacity)

    stream = await engine.stream_pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
        tail_lines=FAST.snapshot_tail_lines * 10,
    )
    try:
        await collect(stream, 2)
    finally:
        await stream.aclose()

    assert cluster.follow_bounds == [(None, FAST.snapshot_tail_lines)]


async def test_a_pod_of_another_workload_is_a_404(capacity):
    """The check that matters: owning the workload is not owning every pod.

    All workloads' pods share one namespace, so without this any authenticated
    caller could read any pod's log by naming it.
    """

    class Foreign(OwnedCluster):
        def get(self, kind, name=None, label_selector=None):
            from common.cluster import ResourceKind

            if kind is ResourceKind.POD and name is not None:
                return {
                    "metadata": {
                        "name": name,
                        "labels": {pods_stream.SERVICE_LABEL: "someone-elses-workload"},
                    }
                }
            return super().get(kind, name, label_selector)

    engine = _engine(Foreign({}), capacity)

    with pytest.raises(NotFoundError):
        await engine.stream_pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="victim",
            container="user-container",
            since_seconds=None,
        )
    assert capacity.open_streams == 0


async def test_a_pod_with_no_service_label_at_all_is_a_404(capacity):
    class Unlabelled(OwnedCluster):
        def get(self, kind, name=None, label_selector=None):
            from common.cluster import ResourceKind

            if kind is ResourceKind.POD and name is not None:
                return {"metadata": {"name": name}}
            return super().get(kind, name, label_selector)

    engine = _engine(Unlabelled({}), capacity)

    with pytest.raises(NotFoundError):
        await engine.stream_pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="random",
            container="user-container",
            since_seconds=None,
        )


async def test_a_pod_that_does_not_exist_is_a_404_and_keeps_the_slot(capacity):
    engine = _engine(OwnedCluster({}), capacity)

    with pytest.raises(NotFoundError):
        await engine.stream_pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="ghost",
            container="user-container",
            since_seconds=None,
        )
    assert capacity.open_streams == 0


# --- admission across both stream kinds -------------------------------------


async def test_the_slot_is_held_for_the_stream_and_returned_when_it_ends(capacity):
    cluster = OwnedCluster({"p1": "r1"}, live=True)
    engine = _engine(cluster, capacity)

    stream = await engine.stream_pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
    )
    assert capacity.open_streams == 1  # taken at open, not at the first event
    await collect(stream, 1)
    assert capacity.open_streams == 0


async def test_streams_past_the_limit_are_refused(capacity):
    """Per-pod means a user watching N pods spends N slots - so this bites sooner."""
    cluster = OwnedCluster({"p1": "r1", "p2": "r1"}, live=True)
    engine = _engine(cluster, capacity)
    opened = []
    for pod in ("p1", "p2")[: FAST.max_concurrent]:
        opened.append(
            await engine.stream_pod_logs(
                _offering(),
                "foo",
                _caller(),
                "team",
                pod=pod,
                container="user-container",
                since_seconds=None,
            )
        )
    try:
        with pytest.raises(ServiceUnavailableError):
            await engine.stream_pods(_offering(), "foo", _caller(), "team")
    finally:
        for stream in opened:
            await stream.aclose()
    assert capacity.open_streams == 0


# --- the stats stream's orchestration ---------------------------------------


async def test_stream_stats_takes_the_first_reading_before_opening(capacity):
    engine = _engine(OwnedCluster({}), capacity)
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


# --- follow=false: the same reads, without the stream ------------------------


class SnapshotCluster(OwnedCluster):
    """An OwnedCluster that also answers the one-shot log read."""

    def __init__(self, *a, text="", **kw):
        super().__init__(*a, **kw)
        self.text = text
        self.reads: list[tuple] = []

    def pod_logs(self, pod, *, container, since_seconds=None, limit_bytes=None, tail_lines=None):
        self.reads.append((pod, container, since_seconds, limit_bytes, tail_lines))
        return self.text


async def test_the_snapshot_returns_the_same_lines_the_stream_would(capacity):
    """One shape either way, so a client renders the log the same however it read it."""
    cluster = SnapshotCluster(
        {"p1": "rev-7"},
        text="2024-03-01T10:00:00Z first\n2024-03-01T10:00:01Z second\n",
    )
    engine = _engine(cluster, capacity)

    snap = await engine.pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
        limit_bytes=None,
    )

    assert [line.message for line in snap.lines] == ["first", "second"]
    assert snap.lines[0].time is not None  # split off, as the stream splits it
    assert snap.lines[0].revision == "rev-7"
    assert snap.pod == "p1"
    assert snap.region == "central"


async def test_the_snapshot_passes_its_bounds_to_the_cluster(capacity):
    cluster = SnapshotCluster({"p1": "r1"}, text="x\n")
    engine = _engine(cluster, capacity)

    await engine.pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="queue-proxy",
        since_seconds=30,
        limit_bytes=4096,
    )
    assert cluster.reads == [("p1", "queue-proxy", 30, 4096, FAST.snapshot_tail_lines)]


async def test_the_snapshot_is_bounded_even_when_the_caller_asks_for_nothing(capacity):
    """No caller bound is not an unbounded read: the node may hold tens of MB,
    and parsing it all is the event loop's time and the process's memory."""
    cluster = SnapshotCluster({"p1": "r1"}, text="x\n")
    engine = _engine(cluster, capacity)

    await engine.pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
        limit_bytes=None,
    )
    assert cluster.reads == [
        ("p1", "user-container", None, FAST.snapshot_max_bytes, FAST.snapshot_tail_lines)
    ]


async def test_a_callers_limit_bytes_is_clamped_to_the_ceiling(capacity):
    cluster = SnapshotCluster({"p1": "r1"}, text="x\n")
    engine = _engine(cluster, capacity)

    await engine.pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
        limit_bytes=FAST.snapshot_max_bytes * 10,
    )
    assert cluster.reads[0][3] == FAST.snapshot_max_bytes


async def test_an_empty_log_is_no_lines_rather_than_one_empty_one(capacity):
    engine = _engine(SnapshotCluster({"p1": "r1"}, text=""), capacity)
    snap = await engine.pod_logs(
        _offering(),
        "foo",
        _caller(),
        "team",
        pod="p1",
        container="user-container",
        since_seconds=None,
        limit_bytes=None,
    )
    assert snap.lines == []


async def test_the_snapshot_runs_the_same_pod_ownership_check_as_the_stream(capacity):
    """follow=false must not be a way around the check that matters."""

    class Foreign(SnapshotCluster):
        def get(self, kind, name=None, label_selector=None):
            from common.cluster import ResourceKind

            if kind is ResourceKind.POD and name is not None:
                return {
                    "metadata": {
                        "name": name,
                        "labels": {pods_stream.SERVICE_LABEL: "someone-elses-workload"},
                    }
                }
            return super().get(kind, name, label_selector)

    cluster = Foreign({})
    engine = _engine(cluster, capacity)

    with pytest.raises(NotFoundError):
        await engine.pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="victim",
            container="user-container",
            since_seconds=None,
            limit_bytes=None,
        )
    assert cluster.reads == [], "the log was read before the check"


async def test_the_snapshot_hides_another_group_s_workload_as_a_404(capacity):
    engine = _engine(SnapshotCluster({"p1": "r1"}), capacity, owner_group="someone-else")

    with pytest.raises(NotFoundError):
        await engine.pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="p1",
            container="user-container",
            since_seconds=None,
            limit_bytes=None,
        )


async def test_neither_snapshot_spends_a_stream_slot(capacity):
    """They end, so they are not streams and must not be rationed as one."""
    cluster = SnapshotCluster({"p1": "r1"}, text="x\n")
    engine = _engine(cluster, capacity)

    # Enough calls to exhaust the (tiny) admission budget several times over.
    for _ in range(FAST.max_concurrent * 3):
        await engine.pods(_offering(), "foo", _caller(), "team")
        await engine.pod_logs(
            _offering(),
            "foo",
            _caller(),
            "team",
            pod="p1",
            container="user-container",
            since_seconds=None,
            limit_bytes=None,
        )
    assert capacity.open_streams == 0


async def test_the_pods_snapshot_is_the_roster_the_stream_would_have_sent(capacity):
    cluster = SnapshotCluster({"p2": "r1", "p1": "r1"})
    engine = _engine(cluster, capacity)

    roster = await engine.pods(_offering(), "foo", _caller(), "team")

    assert [p.pod for p in roster.pods] == ["p1", "p2"]
    assert roster.region == "central"
    assert roster.name == "foo"


async def test_the_pods_snapshot_hides_another_group_s_workload_as_a_404(capacity):
    engine = _engine(SnapshotCluster({}), capacity, owner_group="someone-else")

    with pytest.raises(NotFoundError):
        await engine.pods(_offering(), "foo", _caller(), "team")


async def test_the_buffer_is_bounded_by_bytes_as_well_as_lines():
    """queue_size alone is no bound for a pod writing without newlines: each
    "line" arrives as a ~1MB piece, and a thousand of those is a gigabyte."""
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=1000, max_bytes=100)

    buf.put("x" * 80)  # fits
    buf.put("y" * 80)  # would blow the byte budget -> dropped and counted
    buf.put("z" * 30)  # 80+30 still over budget -> dropped too
    assert buf.take_dropped() == 2
    assert [len(i) for i in buf.drain()] == [80]

    # A frame too big for an EMPTY buffer is refused too, not waved through: one
    # frame is NOT bounded by MAX_LINE_BYTES (JSON escaping expands the raw line
    # several times over), so admitting it would leave the budget advisory.
    buf.put("w" * 500)
    assert buf.drain() == []
    assert buf.take_dropped() == 1

    # Draining resets the byte budget along with the items.
    buf.put("a" * 80)
    assert buf.take_dropped() == 0


async def test_the_byte_budget_counts_bytes_not_characters():
    """The budget is a memory bound, and a workload logging outside ASCII spends
    up to four bytes a character - counted as one, the buffer holds four times
    what was configured."""
    buf = logs_stream._Buffer(asyncio.get_running_loop(), maxsize=1000, max_bytes=100)

    buf.put("א" * 40)  # 40 characters, 80 bytes: fits
    buf.put("א" * 40)  # another 80 bytes would not
    assert buf.take_dropped() == 1
    assert len(buf.drain()) == 1


async def test_the_rollover_reports_lines_dropped_since_the_last_tick(capacity):
    """The deadline path must not present the log as gapless when it wasn't:
    drops accumulated since the last tick are reported before the `end`."""

    class Endless:
        def __init__(self):
            self._open = True

        def lines(self):
            i = 0
            while self._open:
                i += 1
                yield f"line {i}"

        def close(self):
            self._open = False

    class FirehoseCluster(FakeCluster):
        def follow_pod_logs(self, pod, *, container, since_seconds=None, tail_lines=None):
            return Endless()

    config = FAST.model_copy(update={"max_seconds": 1, "queue_size": 2, "heartbeat_seconds": 0.02})
    events = [e async for e in _follow(FirehoseCluster({"p1": "r1"}), capacity, config=config)]

    assert names(events)[-1] == "end"
    assert "time limit" in events[-1].data.reason
    warnings = [e for e in events if not isinstance(e, str) and e.name == "warning"]
    assert warnings, "an endless producer against queue_size=2 must have dropped lines"
    assert all(w.data.droppedLines > 0 for w in warnings)
