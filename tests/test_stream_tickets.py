"""Stream tickets: what one is worth, and every way one is refused."""

from __future__ import annotations

import base64
import json
import time

import pytest
from cloudlet_apis.auth import Principal
from cloudlet_apis.errors import ServiceUnavailableError, UnauthenticatedError

from api.auth.tickets import StreamTickets
from api.models.stream import StreamTicketRequest

KEY = "unit-test-signing-key"  # noqa: S105 - not a credential, a fixture
PATH = "/api/v1/groups/team/functions/foo/logs/pods/foo-team-00001-abcde"


def _principal(**over):
    base = dict(subject="s-1", username="alice", groups={"team": "/GGD-Team"}, is_admin=False)
    return Principal(**{**base, **over})


def _payload(ticket: str) -> dict:
    raw = ticket.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


# --- minting ---------------------------------------------------------------


def test_a_ticket_round_trips_to_the_principal_that_minted_it():
    tickets = StreamTickets(KEY)
    who = _principal()

    resolved = tickets.verify(tickets.mint(who, PATH).value, PATH)

    assert resolved.subject == who.subject
    assert resolved.username == who.username
    # The SSO spelling survives, not just the normalized key: the stream has to
    # resolve the same Principal the header would have.
    assert resolved.groups == {"team": "/GGD-Team"}
    assert resolved.is_admin is False


def test_an_admin_ticket_stays_an_admin():
    tickets = StreamTickets(KEY)
    resolved = tickets.verify(tickets.mint(_principal(is_admin=True), PATH).value, PATH)
    assert resolved.is_admin is True


def test_the_ticket_carries_no_token_and_no_secret():
    """The whole point: what lands in the URL is not the caller's SSO token."""
    tickets = StreamTickets(KEY)
    ticket = tickets.mint(_principal(), PATH)
    assert KEY not in ticket.value
    assert set(_payload(ticket.value)) == {"v", "sub", "usr", "grp", "adm", "pth", "exp"}


def test_expiry_is_the_configured_ttl():
    tickets = StreamTickets(KEY, ttl_seconds=30)
    ticket = tickets.mint(_principal(), PATH)
    assert 25 <= ticket.expires_at - int(time.time()) <= 30


# --- refusal ---------------------------------------------------------------


def test_a_ticket_is_refused_on_a_different_path():
    """The binding that stops one pod's ticket reading another pod's logs."""
    tickets = StreamTickets(KEY)
    ticket = tickets.mint(_principal(), PATH).value

    with pytest.raises(UnauthenticatedError):
        tickets.verify(ticket, "/api/v1/groups/team/functions/foo/logs/pods/another-pod")


def test_a_ticket_signed_with_another_key_is_refused():
    forged = StreamTickets("someone-elses-key").mint(_principal(is_admin=True), PATH).value
    with pytest.raises(UnauthenticatedError):
        StreamTickets(KEY).verify(forged, PATH)


def test_an_expired_ticket_is_refused():
    tickets = StreamTickets(KEY, ttl_seconds=1)
    ticket = tickets.mint(_principal(), PATH).value
    # Signed over the expiry, so the clock is the only thing that has to move.
    assert tickets.verify(ticket, PATH)
    time.sleep(1.1)
    with pytest.raises(UnauthenticatedError):
        tickets.verify(ticket, PATH)


def test_a_tampered_payload_is_refused():
    """Editing the claims invalidates the signature over them - including is_admin."""
    tickets = StreamTickets(KEY)
    ticket = tickets.mint(_principal(), PATH).value
    payload, _, signature = ticket.partition(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["adm"] = True
    escalated = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    with pytest.raises(UnauthenticatedError):
        tickets.verify(f"{escalated}.{signature}", PATH)


@pytest.mark.parametrize("junk", ["", ".", "nodot", "a.b", "....", "!!!.???"])
def test_a_malformed_ticket_is_refused_rather_than_raising(junk):
    with pytest.raises(UnauthenticatedError):
        StreamTickets(KEY).verify(junk, PATH)


def test_every_refusal_says_the_same_thing():
    """Expired, forged and wrong-path must not be distinguishable to a prober."""
    tickets = StreamTickets(KEY, ttl_seconds=1)
    good = tickets.mint(_principal(), PATH).value
    forged = StreamTickets("other").mint(_principal(), PATH).value
    time.sleep(1.1)

    messages = set()
    for ticket, path in ((good, PATH), (forged, PATH), (good, "/other")):
        with pytest.raises(UnauthenticatedError) as caught:
            tickets.verify(ticket, path)
        messages.add(caught.value.message)
    assert len(messages) == 1


# --- disabled --------------------------------------------------------------


def test_no_key_disables_minting_rather_than_signing_with_an_empty_one():
    tickets = StreamTickets("")
    assert tickets.enabled is False
    with pytest.raises(ServiceUnavailableError):
        tickets.mint(_principal(), PATH)


def test_no_key_refuses_every_ticket_including_one_signed_with_the_empty_key():
    """An empty key must not become a key everyone knows."""
    signed_with_empty = StreamTickets("x")
    signed_with_empty._key = b""  # what a naive implementation would sign with
    forged = StreamTickets("")
    forged._key = b"anything"
    ticket = forged.mint(_principal(), PATH).value

    with pytest.raises(UnauthenticatedError):
        StreamTickets("").verify(ticket, PATH)


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
