"""Server-Sent Events: the bounds, the framing, and the streams themselves.

* :mod:`~api.services.streams.capacity` - the pool held-open work runs on, and
  the gate that stops more streams being admitted than it can serve.
* :mod:`~api.services.streams.sse` - the wire format, and the event type the
  streams yield.
* :mod:`~api.services.streams.logs` - following pod logs on the local region.
* :mod:`~api.services.streams.pods` - the pod roster of one workload on the
  local region, on an interval.
* :mod:`~api.services.streams.stats` - the live rollup, on an interval.

The orchestration that authorizes a stream and builds its first event lives with
every other read, on :class:`~api.services.workloads.WorkloadService`.
"""

from api.services.streams.capacity import StreamCapacity, run_on
from api.services.streams.sse import HEADERS, MEDIA_TYPE, StreamEvent, preamble, render

__all__ = [
    "HEADERS",
    "MEDIA_TYPE",
    "StreamCapacity",
    "StreamEvent",
    "preamble",
    "render",
    "run_on",
]
