"""Workloads live in one namespace per group, and a deploy waits for it.

The cutover's own seams: where a request resolves to, that the tenant
controller is asked to provision before anything is written, and that the
answer is believed only when every region says Ready. The last one is the
reason this call exists at all - a create that lands in a namespace nobody
prepared is exactly what it is meant to prevent, and "the check could not be
run" has to read as failure, not as consent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from api.services import tenant_namespace
from api.services.tenant_namespace import provision_namespace
from common.config import TenantNamespaceConfig
from common.errors import (
    ProvisioningRejectedError,
    ServiceUnavailableError,
    ValidationError,
)
from tests.factories import _FakeCluster, _workload_service


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        return json.loads(json.dumps(self._payload))

    @property
    def text(self):
        return json.dumps(self._payload)


class _Client:
    """Stands in for the module's shared httpx client, recording each call."""

    calls: list[dict] = []
    is_closed = False

    def __init__(self, response=None, boom=None):
        self._response = response
        self._boom = boom

    def __call__(self, **kwargs):
        self._init_kwargs = kwargs
        return self

    def install(self, monkeypatch):
        """Stand in for httpx.AsyncClient, and drop any cached client."""
        monkeypatch.setattr(tenant_namespace.httpx, "AsyncClient", self)
        monkeypatch.setattr(tenant_namespace, "_client", None)
        return self

    async def put(self, url, headers=None):
        kwargs = getattr(self, "_init_kwargs", {})
        type(self).calls.append({"url": url, "headers": headers or {}, **kwargs})
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
    mine = svc.targets_for("team")
    theirs = svc.targets_for("other")

    assert [c.namespace for c in mine] == ["team-serverless"]
    assert [c.namespace for c in theirs] == ["other-serverless"]
    # ...and their default hosts still differ, because the host keeps the pair.
    assert svc.host_for("app", None, "team") != svc.host_for("app", None, "other")


# --- the provision call ----------------------------------------------------


async def test_provision_puts_to_the_controller_and_accepts_a_ready_answer(monkeypatch):
    _Client(response=_ok(("central", "south"))).install(monkeypatch)
    await provision_namespace("payments", _config(), verify=False)

    assert len(_Client.calls) == 1
    assert _Client.calls[0]["url"] == "http://tenant-controller:8080/groups/payments/namespace"


async def test_a_configured_token_is_presented(monkeypatch):
    _Client(response=_ok()).install(monkeypatch)
    await provision_namespace("payments", _config(token="s3cret"))
    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer s3cret"


async def test_the_client_is_built_once_and_reused(monkeypatch):
    """Connection pooling, and the CA bundle parsed once - not per create."""
    client = _Client(response=_ok()).install(monkeypatch)
    await provision_namespace("payments", _config())
    monkeypatch.setattr(
        tenant_namespace.httpx, "AsyncClient", _boom_factory
    )  # a second construction would raise
    await provision_namespace("payments", _config())
    assert len(_Client.calls) == 2
    assert client is tenant_namespace._client


def _boom_factory(**_kwargs):
    raise AssertionError("a second AsyncClient was constructed")


async def test_no_controller_configured_skips_the_call(monkeypatch):
    """A dev cluster has no tenant controller; the namespace is whatever was made by hand."""
    _Client(response=_ok()).install(monkeypatch)
    await provision_namespace("payments", TenantNamespaceConfig())
    assert _Client.calls == []


async def test_an_unreachable_controller_fails_the_deploy_closed(monkeypatch):
    """A check that could not be run has not passed - the pre-flight rule."""
    _Client(boom=RuntimeError("connection refused")).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError, match="could not prepare the namespace"):
        await provision_namespace("payments", _config())


async def test_a_partial_answer_is_not_a_success(monkeypatch):
    """The tenant controller answers 200 with per-region rows; a deploy writes to
    all of them, so anything short of every region Ready would put a workload in
    a namespace that is not prepared."""
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
        await provision_namespace("payments", _config())


async def test_a_502_from_the_controller_is_a_503_to_the_caller(monkeypatch):
    """Nothing landed anywhere. The caller's deploy is refused, not accepted."""
    _Client(response=_Response({"error": {}}, status=502)).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError):
        await provision_namespace("payments", _config())


async def test_a_4xx_from_the_controller_is_not_called_retryable(monkeypatch):
    """The controller answered and refused: a config mismatch between the two
    ends, not an outage. "Retry shortly" would send the operator hunting an
    availability problem instead of a config diff."""
    refusal = _Response({"error": {"message": "group 'x' is too long"}}, status=422)
    _Client(response=refusal).install(monkeypatch)
    with pytest.raises(ProvisioningRejectedError, match="too long"):
        await provision_namespace("payments", _config())


async def test_a_200_with_no_region_rows_is_not_a_provisioned_namespace(monkeypatch):
    """An answer this code does not understand is a failed check, not a passed one.

    Defaulting the missing list to empty would have made "nothing unconverged"
    true by vacuum, letting a create through into a namespace nobody confirmed.
    """
    _Client(response=_Response({"namespace": "payments-serverless"})).install(monkeypatch)
    with pytest.raises(ServiceUnavailableError, match="no per-region answer"):
        await provision_namespace("payments", _config())


# --- placement: once per accepted request, never in the cluster probes ------


def _svc_with_controller():
    svc = _workload_service({"region-a": _FakeCluster("region-a")})
    svc.settings.tenant_namespaces.controller_url = "http://tenant-controller:8080"
    return svc


async def test_a_deploy_is_refused_when_the_namespace_cannot_be_provisioned(monkeypatch):
    _Client(boom=RuntimeError("connection refused")).install(monkeypatch)
    svc = _svc_with_controller()

    with pytest.raises(ServiceUnavailableError, match="could not prepare the namespace"):
        await svc.provision_namespace("team")


async def test_the_cluster_probes_never_call_the_controller(monkeypatch):
    """Provisioning happens once per accepted request; the probes - which the
    apply path re-runs right before the mutation - must not repeat the round
    trip."""
    _Client(response=_ok()).install(monkeypatch)
    svc = _svc_with_controller()

    await svc.assert_deployable("app", "team", svc.targets_for("team"), require_absent=True)
    await svc.assert_host_available(
        "app-team.serverless.example.com", "app", "team", svc.targets_for("team")
    )

    assert _Client.calls == []


# --- the accept paths are actually wired to the controller ------------------


def _container_service(monkeypatch, client, existing=None):
    from api.services.container import ContainerService
    from tests.factories import _ApplyCluster

    client.install(monkeypatch)
    cluster = _ApplyCluster("region-a", dict(existing or {}))
    svc = _workload_service({"region-a": cluster})
    svc.settings.tenant_namespaces.controller_url = "http://tenant-controller:8080"
    return ContainerService(svc), cluster


def _existing_ksvc(name, group):
    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.config import CABundleConfig

    ca = CABundleConfig()
    return build_ksvc(
        name=name,
        group=group,
        owner="alice",
        image="reg/x:1",
        offering="container",
        host=f"{name}-{group}.serverless.example.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        ca_config_map=ca.config_map,
        ca_mount_path=ca.mount_path,
        ca_file=ca.file,
    )


async def test_a_create_provisions_before_it_is_accepted(monkeypatch):
    """The wiring itself: dropping the provision call from accept would pass
    every isolated test while creates landed in unprovisioned namespaces."""
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate

    csvc, _cluster = _container_service(
        monkeypatch, _Client(response=_ok(("region-a",), namespace="team-serverless"))
    )
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.accept(
        "team", ContainerCreate(name="app", image="reg/x:1", port=8080), user, BackgroundTasks()
    )

    assert [c["url"] for c in _Client.calls] == [
        "http://tenant-controller:8080/groups/team/namespace"
    ]


async def test_an_update_provisions_too(monkeypatch):
    """A region added after the group's first create gets its namespace here."""
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerUpdate

    csvc, _cluster = _container_service(
        monkeypatch,
        _Client(response=_ok(("region-a",), namespace="team-serverless")),
        existing={"app": _existing_ksvc("app", "team")},
    )
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.accept_update(
        "team", "app", ContainerUpdate(image="reg/x:2", port=8080), user, BackgroundTasks()
    )

    assert [c["url"] for c in _Client.calls] == [
        "http://tenant-controller:8080/groups/team/namespace"
    ]


async def test_an_update_tolerates_a_controller_outage(monkeypatch):
    """The workload just loaded proves the namespace exists: an unreachable
    controller must not block an update - or a rollback - of something already
    running. A create still fails closed; a refusal (4xx) still propagates."""
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerUpdate

    csvc, _cluster = _container_service(
        monkeypatch,
        _Client(boom=RuntimeError("connection refused")),
        existing={"app": _existing_ksvc("app", "team")},
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    background = BackgroundTasks()

    resp = await csvc.accept_update(
        "team", "app", ContainerUpdate(image="reg/x:2", port=8080), user, background
    )

    assert resp.status == "Pending"
    assert background.tasks, "the update proceeded despite the outage"


async def test_a_rejection_still_blocks_an_update(monkeypatch):
    """Outage tolerance is not rejection tolerance: a 4xx means the two ends
    disagree about configuration, and deploying anyway could land wrong."""
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerUpdate

    refusal = _Response({"error": {"message": "bad token"}}, status=401)
    csvc, _cluster = _container_service(
        monkeypatch,
        _Client(response=refusal),
        existing={"app": _existing_ksvc("app", "team")},
    )
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(ProvisioningRejectedError):
        await csvc.accept_update(
            "team", "app", ContainerUpdate(image="reg/x:2", port=8080), user, BackgroundTasks()
        )
