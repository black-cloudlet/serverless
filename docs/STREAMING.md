# Streaming

Live observability is **per pod and Server-Sent Events**: a roster stream that names a
workload's pods, and a log stream that follows one of them. Not WebSockets - the traffic is
one-directional, SSE is plain HTTP through the existing Route with no upgrade to negotiate,
and browsers reconnect on their own. The endpoints themselves are listed in API.md:
Endpoints; this document is how they behave.

## Contents

- [The streams](#the-streams)
- [`follow=false`](#followfalse)
- [Browsers cannot send an `Authorization` header](#browsers-cannot-send-an-authorization-header)
- [A held-open stream holds a thread](#a-held-open-stream-holds-a-thread)
- [Errors after the first byte](#errors-after-the-first-byte)

## The streams

| Endpoint | Emits | Scope |
|---|---|---|
| `.../{name}/pods` | `pods` events: the roster, one entry per pod, re-pushed as it changes. | Local region only. |
| `.../{name}/logs/pods/{pod}` | `open`, then a `log` event per line, then `end`. `warning` carries `droppedLines`. | Local region only. |
| `.../{name}/stats/stream` | The `/stats` body, once immediately and then every `interval` seconds. | All regions (the rollup). |

```
GET .../{name}/pods                      →  event: pods   {"pods":[{"pod":"…-x2wql", …}]}
                                                   │
                                                   ▼  pick one
GET .../{name}/logs/pods/…-x2wql         →  event: open
                                            event: log    {"time":…, "message":"…"}
                                            event: log    …
                                            event: end    "the pod's log ended…"
```

**The `end` event is not a failure.** A pod's log ending is what a scale-down or a new
revision looks like. A client that treats it as an error shows a red banner for a successful
deploy; one that is told goes back to the `pods` stream and picks the replacement.

### Why per pod, and why streaming is the default

There is no workload-level log follow. It would have to reconcile a *set* of pods that
changes underneath it, which means a per-stream pod cap, an arbitrary rule for which pods win
when a workload is wider than the cap, and a client that still cannot say "just the noisy
one". Per pod, each stream is one pod, one thread, no set to reconcile, and the choice of
what to watch moves to the side that knows what the user is looking at. The cost is that the
client must first learn a pod name, which is what `/pods` is for.

`/pods` defaults to streaming because its answer expires: Knative replaces a workload's pods
on every revision and removes them all on scale-to-zero, so a roster fetched once quietly
stops being true and a client would have to poll it at exactly the cadence this pushes at.

Logs and the roster are **local region only**. A pod name is only useful where its log can be
read, and logs live on the node that wrote them. `/stats` stays multi-region: the rollup is a
cross-region question.

### What the pod roster reports

- A pod with **no metrics reading is still listed**, with `usage: null`. metrics-server has
  not scraped a pod that started a second ago, and that is precisely the pod a client most
  wants to follow.
- **Readiness is reported beside `phase`**, not folded into it. Conflating the two is how a
  UI shows a workload as up while its requests fail.
- **Restarts count the queue-proxy sidecar** even though usage does not: a queue-proxy that
  keeps restarting is a pod that keeps dropping traffic.

### SSE framing

`api.services.streams.sse` owns the wire format, so the stream tests assert on typed events
instead of parsing text. Three rules matter to a client:

- The response headers **disable buffering explicitly** for intermediaries in the path. A
  buffered event stream is indistinguishable from a hung one until the buffer flushes.
- A log line carries the node's timestamp as a field of its own (`LogLine`) rather than
  leaving clients to re-parse the line prefix, in the same timezone as every other API
  timestamp (`createdAt`).
- The streaming `200` is declared by hand in `routers.streaming.RESPONSES`, because FastAPI
  infers a response schema from the return annotation and a `StreamingResponse` has none.

### A requested interval is clamped, never rejected

A stream's `interval` is bounded at both ends: the floor stops one client asking for a
re-read every 50 ms and protects the cluster, the ceiling keeps a quiet connection from
looking dead. Neither is worth a `400` when "as often as this deployment allows" is what the
caller wanted. The first reading of a stats stream is emitted immediately, so the client is
not left with an empty panel until the first interval elapses.

## `follow=false`

Both `/pods` and `/logs/pods/{pod}` take `?follow=false` and answer once, in JSON. It is the
only form available to a caller that cannot hold a connection open, and the architecture has
one: a ServiceNow workflow attaching a failing function's logs to a ticket cannot consume an
event stream. It is on **both** endpoints, because a log snapshot alone would be unreachable -
finding a pod name would still require opening a stream.

**What a snapshot returns is bounded twice.**

| Bound | Value | Meaning |
|---|---|---|
| The node's retained log | - | Kubernetes keeps no ring buffer beyond its rotated file, so a snapshot is the recent past, never the whole history. This is a platform property, and the same limit a follow starts from. |
| `stream.snapshotTailLines` | 2000 | The newest lines, kept from the **tail** - `limitBytes` alone truncates from the *start* of the window and drops what a reader wants. |
| `stream.snapshotMaxBytes` | 2 MiB | A hard byte ceiling; a caller's `limitBytes` is clamped to it. It backstops pathological line lengths, which a line count bounds nothing against. |

The API imposes those bounds rather than leaving them to the caller. A node can hold tens of
megabytes for one container, and an unbounded snapshot is read, parsed and serialized into
one response by the same process that answers the health probes.

Lines are split exactly as the stream splits them, so a client renders one shape either way.

Two things follow from a snapshot being an ordinary request:

1. It takes **no stream slot**. It ends, so rationing it against a pool that exists to bound
   held-open connections would let streams throttle a caller that is not holding one.
2. It runs on the default executor like every other request.

What it does not skip is authorization: both forms go through the same `_pod_authorizer`, so
`follow=false` is not a way around the check that the named pod is this workload's.

## Browsers cannot send an `Authorization` header

`EventSource` is the only way a browser consumes SSE and there is no API to give it a header.
That leaves the credential in the URL, and the SSO token is the wrong thing to put there: it
is valid against every endpoint, it outlives the request, and a URL reaches the router's
access log, this API's own log line and the user's history.

So the token buys a **ticket** instead. `POST /api/serverless/v1/stream-tickets` takes the
bearer token on a request that can carry one and returns an opaque credential worth almost
nothing: **one** stream path, for ~60s, carrying an identity the caller already had.

```
POST /api/serverless/v1/stream-tickets            EventSource(url + "?ticket=…")
  Authorization: Bearer <SSO token>  →   GET …/logs/pods/{pod}?ticket=…
  {"path": "/api/serverless/v1/…/logs/pods/…"}      (no header; none is possible)
```

- The ticket is **HMAC-signed rather than stored**: two replicas serve behind one Route and
  either may take the stream, so a ticket held in the minting process's memory would fail
  about half the time.
- **The path is inside the signature**, so a ticket for one pod's logs cannot be replayed
  against another's. Every refusal - expired, forged, wrong path - returns the same message.
- **Group authorization is not done at minting.** The ticket conveys only who you already
  are, and the stream re-runs the same check the ordinary GET does, so a ticket for a group
  you are not in opens a stream that `404`s.
- `SERVERLESS_STREAM_TICKET_KEY` (Vault → ESO, the same value in every replica and region)
  enables minting. Empty **disables** it, exactly as an empty admin key disables key auth
  (API.md: Static API keys). The streams still accept the `Authorization` header, so a
  `curl -N` follow needs no configuration and only the browser path depends on the secret.

The mechanism - the signer, the mint endpoint, the stream dependency - is `cloudlet_apis.auth`
(`StreamTickets`, `ticket_mint_router`, `stream_auth`), shared with every API on the platform
because `EventSource` sends no header anywhere (API.md: Auth as a shared library). What stays
in this repository is `validate_stream_path` in `api/models/stream.py`, which enumerates the
paths a ticket may be minted for: a bearer credential in a URL should open a listed thing
rather than an inferred one, and those paths are this API's to know.

### Authorizing a pod

Owning the workload is not owning every pod: the caller names one, and every workload's pods
share a namespace. So the log stream checks twice - the KSVC's ownership labels, **and** that
the named pod carries this workload's `serving.knative.dev/service` label. Without the second
check any authenticated user could read any pod in the namespace by guessing its name.

A pod that fails the check is a `404`, identical to one that does not exist, so the response
never confirms that a pod by that name is running. The pod name is also a path segment that
reaches a request to the cluster's API server, so it is constrained at the edge to what
Kubernetes itself accepts as a pod name (`validate_pod_name`, API.md: Validation at the edge).

## A held-open stream holds a thread

The Kubernetes client is synchronous, so following a pod log is a thread **blocked on a
socket for as long as the client stays connected** - not for the length of a request. On the
default executor that `asyncio.to_thread` uses, a handful of idle log tails would occupy the
same threads every create, read and delete needs, and the API would stop answering while
looking healthy.

So streaming owns a pool of its own (`api/services/streams/capacity.py`), and admission is
capped **before** that pool can be exhausted:

| Bound | Default | What it stops |
|-------|---------|---------------|
| `stream.maxConcurrent` | 32 | More streams than the pool can serve. Beyond it: `503` with a retry - being told to come back beats being connected and starved. Streams are per pod, so a client watching four pods spends four; that is why this is far higher than a workload-level cap would be. |
| `stream.queueSize` | 1000 | A pod logging faster than its reader growing the process. Past it, lines are dropped and the gap is **reported** as a `warning` carrying `droppedLines`. |
| `stream.maxSeconds` | 3600 | An immortal stream. It ends itself with an `end` event and the client reconnects, which SSE does unprompted. |

The pool size is **derived** (`maxConcurrent × 2`), not configured: a pool smaller than the
admissions it must serve turns a bound into a stall. Two per stream because a log stream holds
one thread for its whole life, while a `pods` or `stats` stream holds none between ticks and
needs one briefly on each.

**Teardown closes the follow's socket**, the only thing that interrupts a blocking read - a
flag is checked between lines, and a quiet pod produces none - then waits, briefly, before
handing the slot back. The whole generator is guarded, because a client that disconnects
immediately closes it at its **first** suspension point, and those are exactly the streams
that would otherwise leak threads.

**Rendering happens on the follower thread.** A pod can log tens of thousands of lines a
second, and rendering each on the event loop - model dump, frame, one generator hop per line -
starves the loop until the health probes miss and the kubelet restarts the pod, killing every
stream on it; the clients then reconnect onto the surviving replica and take it down the same
way. The follower thread in `api.services.streams.logs` renders the line path and the loop
only forwards bytes, one yield per buffer drain. Adjacent frames are concatenated per drain,
so there is one yield and one transport write per drain instead of per line.

**The hand-off is a hand-rolled bounded buffer, not an `asyncio.Queue`.** Filling a Queue from
a thread means one `call_soon_threadsafe` per line, and scheduling those is itself unbounded:
a pod that outruns its reader would grow the loop's callback queue instead of the buffer.
`_Buffer` is bounded in **bytes as well as lines** (`StreamConfig.queue_max_bytes` beside
`stream.queueSize`), because a pod that writes without newlines makes every "line" a piece of
up to `LogFollow.MAX_LINE_BYTES` (1 MiB), and a thousand of those is a gigabyte.
`MAX_LINE_BYTES` exists one level down for the same reason: a container writing megabytes with
no newline would otherwise grow an unbounded partial-line buffer inside the API process.

**The follow and its stop handle are one locked object.** `_Tail` is touched from both the
follower thread and the event loop; without the lock, a client that disconnects during the
opening round trip leaves a thread following a pod nobody is reading.

**The deadline rollover flushes and reports drops first.** At `stream.maxSeconds`, buffered
lines are delivered and the drop count reported before the `end` event, so the rollover costs
the client no lines that had already arrived and a client reconnecting across it does not read
the log as gapless when lines were skipped.

**The request context is copied into the worker.** `capacity.run_on` copies contextvars into
the thread, as `asyncio.to_thread` does and a bare `run_in_executor` does not; without it the
correlation id the log filter reads is missing from every line a stream's worker writes.

**Slot release is idempotent; shutdown does not wait.** `StreamSlot.release` is safe to call
from whichever owner fires first - the generator's `finally`, the acceptor's error path, the
GC backstop - because a double release must not drive the open count below the true number of
streams. `StreamCapacity.shutdown` does not wait for running threads, which would hold
shutdown open for as long as the slowest peer.

### The Route would cut them

OpenShift's router times a connection out after **30s** by default, which would sever every
stream half a minute in, and the client would reconnect forever without surfacing why. The
chart sets `haproxy.router.openshift.io/timeout` from `api.route.timeout` (default `65m`) and
**fails to render** if it does not exceed `stream.maxSeconds`; the two live in different
sections of `values.yaml`, so the relationship is asserted rather than left to whoever edits
one.

A quiet stream also sends a `:` comment every `stream.heartbeatSeconds`, so nothing in the
path reaps it between events. The timeout applies to the whole Route - OpenShift has no
per-path timeout - and the API bounds its own cluster work with `cluster_op_timeout` (and the
shorter `cluster_read_op_timeout` for reads) regardless.

## Errors after the first byte

Everything that can fail with a status code is settled **before** the response begins: the
slot is taken, the workload and pod are read and authorized, and the first roster or reading
is done. A missing workload is therefore a `404` **envelope** (API.md: Error model), not a
stream that opens and immediately errors.

Once bytes are flowing the status line is spent, so a later failure - the workload deleted,
the region gone - arrives as an `error` event carrying the same `code` the envelope would
have. `/info` publishes that vocabulary, so a client switches on one set of values however
the failure reaches it.
