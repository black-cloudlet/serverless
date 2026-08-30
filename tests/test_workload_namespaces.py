"""Workloads live in one namespace per group, and a create waits for it.

The cutover's own seams: where a request resolves to, that the tenant
tenant controller is asked before anything is written, and that the answer is
believed only when every region says Ready. The last one is the reason this
call exists at all - a create that lands in a namespace nobody prepared is
exactly what it is meant to prevent, and "the check could not be run" has to
read as failure, not as consent.
"""

from __future__ import annotations

import json

import pytest

from api.services.regions import ensure as ensure_mod
from api.services.regions.ensure import ensure_namespace
from common.config import TenantNamespaceConfig
from common.errors import ServiceUnavailableError, ValidationError
from tests.factories import _FakeCluster, _workload_service


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(json.dumps(self._payload))


class _Client:
    """Stands in for httpx.AsyncClient, recording the one call ensure makes."""

    calls: list[dict] = []

    def __init__(self, response=None, boom=None):
        self._response = response
        self._boom = boom

    def __call__(self, **kwargs):
        self._init_kwargs = kwargs
        return self

    def install(self, monkeypatch):
        """Stand in for httpx.AsyncClient in the module under test."""
        monkeypatch.setattr(ensure_mod.httpx, "AsyncClient", self)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def put(self, url, headers=None):
        type(self).calls.append({"url": url, "headers": headers or {}, **self._init_kwargs})
        if self._boom is not None:
            raise self._boom
        return self._response


@pytest.fixture(autouse=True)
def _reset_calls():
    _Client.calls = []
    yield
    _Client.calls = []


def _ok(regions=("central",), namespace="payments-serverless"):
    return _Response(
        {
            "group": "payments",
            "namespace": namespace,
            "templateHash": "abc123",
            "regions": [{"region": r, "status": "Ready", "message": None} for r in regions],
        }
    )


def _config(**overrides) -> TenantNamespaceConfig:
    return TenantNamespaceConfig(controller_url="http://tenant-controller:8080", **overrides)


# --- resolution ------------------------------------------------------------


def test_a_group_resolves_to_its_own_namespace():
    svc = _workload_service({})
    assert svc.namespace_for("payments") == "payments-serverless"
    assert svc.namespace_for("other") == "other-serverless"


def test_a_group_that_cannot_name_a_namespace_is_refused_at_accept_time():
    """Not left for the tenant controller to reject after the request is accepted."""
    svc = _workload_service({})
    with pytest.raises(ValidationError, match="reserved"):
        svc.namespace_for("kube-system")
    with pytest.raises(ValidationError, match="too long"):
        svc.namespace_for("g" * 60)


def test_the_same_name_in_two_groups_lands_in_two_namespaces():
    """What the whole change buys: `app` stops being one global name."""
    svc = _workload_service({"region-a": _FakeCluster("region-a")})
    mine = svc.deployer.resolve_targets(None, svc.namespace_for("team"))
    theirs = svc.deployer.resolve_targets(None, svc.namespace_for("other"))

    assert [c.namespace for c in mine] == ["team-serverless"]
    assert [c.namespace for c in theirs] == ["other-serverless"]
    # ...and their default hosts still differ, because the host keeps the pair.
    assert svc.host_for("app", None, "team") != svc.host_for("app", None, "other")


# --- the ensure call -------------------------------------------------------


async def test_ensure_puts_to_the_controller_and_accepts_a_ready_answer(monkeypatch):
    client = _Client(response=_ok(("central", "south")))
    client.install(monkeypatch)
    await ensure_namespace("payments", _config(), verify=False)

    assert len(_Client.calls) == 1
    assert _Client.calls[0]["url"] == "http://tenant-controller:8080/groups/payments/namespace"


async def test_a_configured_token_is_presented(monkeypatch):
    client = _Client(response=_ok())
    client.install(monkeypatch)
    await ensure_namespace("payments", _config(token="s3cret"))
    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer s3cret"


async def test_no_controller_configured_skips_the_call(monkeypatch):
    """A dev cluster has no tenant controller; the namespace is whatever was made by hand."""
    client = _Client(response=_ok())
    client.install(monkeypatch)
    await ensure_namespace("payments", TenantNamespaceConfig())
    assert _Client.calls == []


async def test_an_unreachable_controller_fails_the_create_closed(monkeypatch):
    """A check that could not be run has not passed - the pre-flight rule."""
    _Client(boom=RuntimeError("connection refused")).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError, match="could not prepare the namespace"):
        await ensure_namespace("payments", _config())


async def test_a_partial_ensure_is_not_a_success(monkeypatch):
    """The tenant controller answers 200 with per-region rows; a create writes to all
    of them, so anything short of every region Ready would put a workload in a
    namespace that is not prepared."""
    partial = _Response(
        {
            "group": "payments",
            "namespace": "payments-serverless",
            "templateHash": "abc123",
            "regions": [
                {"region": "central", "status": "Ready", "message": None},
                {"region": "south", "status": "Failed", "message": "apiserver down"},
            ],
        }
    )
    _Client(response=partial).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError, match="south"):
        await ensure_namespace("payments", _config())


async def test_a_502_from_the_controller_is_a_503_to_the_caller(monkeypatch):
    """Nothing landed anywhere. The caller's create is refused, not accepted."""
    _Client(response=_Response({"error": {}}, status=502)).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError):
        await ensure_namespace("payments", _config())


# --- the create path actually waits on it ----------------------------------


def _svc_with_controller():
    svc = _workload_service({"region-a": _FakeCluster("region-a")})
    svc.settings.tenant_namespaces.controller_url = "http://tenant-controller:8080"
    return svc


async def test_a_create_asks_for_the_namespace_before_it_checks_anything(monkeypatch):
    """Order matters: there is no point proving a name is free in a namespace
    that does not exist yet."""
    _Client(response=_ok(("region-a",), namespace="team-serverless")).install(monkeypatch)
    svc = _svc_with_controller()

    await svc.assert_deployable(
        "app", "team", svc.deployer.resolve_targets(None, "team-serverless"), require_absent=True
    )

    assert [c["url"] for c in _Client.calls] == [
        "http://tenant-controller:8080/groups/team/namespace"
    ]


async def test_a_create_is_refused_when_the_namespace_cannot_be_ensured(monkeypatch):
    _Client(boom=RuntimeError("connection refused")).install(monkeypatch)
    svc = _svc_with_controller()

    with pytest.raises(ServiceUnavailableError, match="could not prepare the namespace"):
        await svc.assert_deployable(
            "app",
            "team",
            svc.deployer.resolve_targets(None, "team-serverless"),
            require_absent=True,
        )


async def test_an_update_does_not_ask_again(monkeypatch):
    """Its namespace exists by definition - the workload is already running there."""
    _Client(response=_ok()).install(monkeypatch)
    svc = _svc_with_controller()

    await svc.assert_host_available(
        "app-team.serverless.example.com",
        "app",
        "team",
        svc.deployer.resolve_targets(None, "team-serverless"),
    )

    assert _Client.calls == []


async def test_a_200_with_no_region_rows_is_not_a_converged_namespace(monkeypatch):
    """An answer this code does not understand is a failed check, not a passed one.

    Defaulting the missing list to empty would have made "nothing unconverged"
    true by vacuum, letting a create through into a namespace nobody confirmed.
    """
    _Client(response=_Response({"namespace": "payments-serverless"})).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError, match="no per-region answer"):
        await ensure_namespace("payments", _config())
