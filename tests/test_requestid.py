"""Request-correlation-id middleware and its surfacing in the error envelope."""

import re

from fastapi.testclient import TestClient

from api.main import create_app
from common.requestid import REQUEST_ID_HEADER, _adopt_or_mint, get_request_id

_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")


def _client():
    return TestClient(create_app())


def test_error_envelope_carries_a_generated_request_id_matching_the_header():
    c = _client()
    r = c.get("/api/v1/does-not-exist")  # 404 framework error, no auth needed
    assert r.status_code == 404
    rid = r.json()["error"]["requestId"]
    # a real, generated id (no longer null) echoed in the response header
    assert rid and _UUID_HEX.match(rid)
    assert r.headers[REQUEST_ID_HEADER] == rid


def test_inbound_openshift_request_id_is_adopted():
    c = _client()
    incoming = "f1a2b3c4-d5e6-7890-abcd-ef0123456789"  # e.g. from the OpenShift router
    r = c.get("/api/v1/does-not-exist", headers={REQUEST_ID_HEADER: incoming})
    assert r.status_code == 404
    # the same id flows through the envelope and back out the response header
    assert r.json()["error"]["requestId"] == incoming
    assert r.headers[REQUEST_ID_HEADER] == incoming


def test_malformed_inbound_id_is_replaced_with_a_fresh_one():
    c = _client()
    bad = "not a valid id -- has spaces and is way too long " * 5
    r = c.get("/api/v1/does-not-exist", headers={REQUEST_ID_HEADER: bad})
    rid = r.json()["error"]["requestId"]
    assert rid != bad and _UUID_HEX.match(rid)  # sanitized -> minted
    assert r.headers[REQUEST_ID_HEADER] == rid


def test_success_response_also_carries_a_request_id_header():
    c = _client()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert _UUID_HEX.match(r.headers[REQUEST_ID_HEADER])


def test_each_request_gets_a_distinct_id():
    c = _client()
    a = c.get("/healthz").headers[REQUEST_ID_HEADER]
    b = c.get("/healthz").headers[REQUEST_ID_HEADER]
    assert a != b


def test_adopt_or_mint_unit():
    assert _adopt_or_mint("good-id_123") == "good-id_123"  # valid -> kept
    assert _adopt_or_mint("has space") != "has space"  # invalid -> minted
    assert _adopt_or_mint(None) and _UUID_HEX.match(_adopt_or_mint(None))  # empty -> minted


def test_get_request_id_is_dash_outside_a_request():
    assert get_request_id() == "-"


async def test_non_http_scope_passes_through_untouched():
    from common.requestid import RequestIDMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    # A lifespan (non-http) scope must be forwarded verbatim - no state/header work.
    scope = {"type": "lifespan"}
    await RequestIDMiddleware(app)(scope, None, None)
    assert seen["scope"] is scope
    assert "state" not in scope
