"""Which paths this API will mint a stream ticket for.

The ticket mechanism itself - signing, expiry, every way one is refused - moved
to ``cloudlet_apis.auth.tickets`` and is tested there. What stays here is the
half that is deliberately *not* shared: a ticket is a bearer credential in a
URL, so the set of paths worth minting for is enumerated by the API that knows
them (docs/DESIGN.md in cloudlet-apis - Stream tickets).
"""

from __future__ import annotations

import pytest

from api.models.stream import StreamTicketRequest

# --- the path a ticket may be minted for -----------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/groups/team/functions/foo/pods",
        "/api/v1/groups/team/functions/foo/stats/stream",
        "/api/v1/groups/team/functions/foo/logs/pods/foo-team-00001-abcde",
        "/api/v1/groups/team/containers/foo/pods",
        "/api/v1/groups/team/containers/foo/stats/stream",
        "/api/v1/groups/team/containers/foo/logs/pods/foo-team-00001-abcde",
    ],
)
def test_the_streaming_paths_are_accepted(path):
    assert StreamTicketRequest(path=path).path == path


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/groups/team/functions/foo",  # not a stream
        "/api/v1/groups/team/functions/foo/logs",  # no such endpoint any more
        "/api/v1/groups/team/functions/foo/logs/stream",  # the removed shape
        "/api/v1/groups/team/functions/foo/logs/pods",  # a pod is required
        "/api/v1/groups/team/functions/foo/pods?ticket=x",  # query included
        "/api/v1/groups/team/widgets/foo/pods",  # not an offering
        "api/v1/groups/team/functions/foo/pods",  # not anchored
        "/api/v1/groups/team/functions/foo/pods/../../delete",  # traversal
        "/api/v1/groups/team/functions/foo/logs/pods/a/b",  # two segments
        "",
    ],
)
def test_anything_else_is_rejected(path):
    with pytest.raises(ValueError):
        StreamTicketRequest(path=path)
