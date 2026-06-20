import pytest

from app.auth.claims import principal_from_claims
from app.core.config import SSOConfig, Settings, SiteConfig
from app.core.errors import ValidationError, SiteTotalFailure
from app.models.common import SiteStatus
from app.services.deployer import Deployer, aggregate, status_code_for


def test_principal_from_claims_strips_and_detects_admin():
    cfg = SSOConfig(groups_claim="groups", admin_groups=["platform-admins"])
    p = principal_from_claims(
        {"sub": "u1", "preferred_username": "alice", "groups": ["/team-a", "/platform-admins"]},
        cfg,
    )
    assert p.username == "alice"
    assert p.groups == ["team-a", "platform-admins"]
    assert p.is_admin is True
    assert p.can_access_group("anything") is True


def test_principal_non_admin_scope():
    cfg = SSOConfig(admin_groups=[])
    p = principal_from_claims({"sub": "u", "groups": "team-a"}, cfg)
    assert p.groups == ["team-a"]
    assert p.can_access_group("team-a") is True
    assert p.can_access_group("team-b") is False


def _settings_with_admin_key(raw_key, admin_groups=("platform-admins",)):
    return Settings(
        auth_enabled=True,
        admin_api_key=raw_key,
        sso=SSOConfig(admin_groups=list(admin_groups)),
    )


def test_require_auth_via_bearer_admin_key():
    from types import SimpleNamespace

    from app.auth.deps import require_auth
    from app.core.errors import UnauthenticatedError

    settings = _settings_with_admin_key("opaque-s3cret")
    # Opaque admin key in the standard Authorization: Bearer header.
    req = SimpleNamespace(headers={"Authorization": "Bearer opaque-s3cret"})
    p = require_auth(req, settings, validator=None)  # validator unused for opaque key
    assert p.username == "admin" and p.is_admin is True
    assert p.groups == ["platform-admins"]

    # An unrecognised, non-JWT token must be rejected, not silently allowed.
    bad = SimpleNamespace(headers={"Authorization": "Bearer nope"})
    with pytest.raises(UnauthenticatedError):
        require_auth(bad, settings, validator=None)


def test_oidc_discovery_resolved_once_and_client_reused(monkeypatch):
    from app.auth.oidc import TokenValidator

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jwks_uri": "https://sso.internal/jwks"}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr("app.auth.oidc.httpx.get", fake_get)

    v = TokenValidator(SSOConfig())
    c1 = v._client()
    c2 = v._client()
    assert c1 is c2  # one PyJWKClient, reused across requests
    assert calls["n"] == 1  # discovery hit exactly once, not per request
    assert c1.uri == "https://sso.internal/jwks"  # JWKS URI came from discovery


def test_oidc_discovery_failure_is_service_unavailable(monkeypatch):
    import httpx

    from app.auth.oidc import TokenValidator
    from app.core.errors import ServiceUnavailableError

    def boom(url, timeout=None):
        raise httpx.ConnectError("sso down")

    monkeypatch.setattr("app.auth.oidc.httpx.get", boom)

    v = TokenValidator(SSOConfig())
    with pytest.raises(ServiceUnavailableError):
        v._client()


def test_aggregate_all_ok():
    statuses = [SiteStatus(site="a", status="Ready"), SiteStatus(site="b", status="Ready")]
    assert aggregate(statuses, "Ready") == "Ready"
    assert status_code_for("Ready", created=True) == 201


def test_aggregate_partial():
    statuses = [
        SiteStatus(site="a", status="Ready"),
        SiteStatus(site="b", status="Failed", error="boom"),
    ]
    assert aggregate(statuses, "Ready") == "Degraded"
    assert status_code_for("Degraded", created=True) == 207


def test_aggregate_total_failure():
    statuses = [SiteStatus(site="a", status="Failed", error="x")]
    with pytest.raises(SiteTotalFailure):
        aggregate(statuses, "Ready")


def _settings_with_sites():
    return Settings(
        sites=[
            SiteConfig(name="site-a", cluster="site-a-0"),
            SiteConfig(name="site-b", cluster="site-b-0"),
        ]
    )


def test_global_cert_and_ca_paths():
    s = Settings(client_cert_dir="/etc/serverless/client")
    assert s.client_cert_file == "/etc/serverless/client/tls.crt"
    assert s.client_key_file == "/etc/serverless/client/tls.key"
    assert s.ca_bundle.file == "/etc/ssl/certs/ca-bundle.crt"


def test_resolve_targets_default_all():
    d = Deployer(_settings_with_sites())
    assert [z.site for z in d.resolve_targets(None)] == ["site-a", "site-b"]


def test_resolve_targets_unknown_site():
    d = Deployer(_settings_with_sites())
    with pytest.raises(ValidationError):
        d.resolve_targets(["site-c"])


async def test_fanout_captures_per_site_errors():
    d = Deployer(_settings_with_sites())

    def fn(cluster):
        if cluster.site == "site-b":
            raise RuntimeError("kaboom")
        return SiteStatus(site=cluster.site, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_site = {s.site: s for s in statuses}
    assert by_site["site-a"].status == "Ready"
    assert by_site["site-b"].error == "kaboom"


async def test_fanout_times_out_unreachable_site():
    import time

    d = Deployer(_settings_with_sites())
    d._op_timeout = 0.05  # tighten for the test

    def fn(cluster):
        if cluster.site == "site-b":
            time.sleep(0.5)  # simulate an unreachable/slow cluster
        return SiteStatus(site=cluster.site, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_site = {s.site: s for s in statuses}
    # the healthy site still returns; the slow one is reported, not blocking
    assert by_site["site-a"].status == "Ready"
    assert by_site["site-b"].status == "Timeout"
    assert by_site["site-b"].error is not None


class _FakeCluster:
    def __init__(self, name, existing=None):
        self.name = name
        self.site = name
        self._existing = existing or {}

    def get(self, kind, name, namespace=None):
        from app.core.errors import NotFoundError as _NF

        if name in self._existing:
            return self._existing[name]
        raise _NF(f"{name} not found")


def _workload_service(clusters):
    from app.services.builder import FuncBuilder
    from app.services.workloads import WorkloadService

    settings = _settings_with_sites()
    d = Deployer(settings)
    d._clusters = clusters  # inject fakes (name -> _FakeCluster)
    return WorkloadService(settings, d, FuncBuilder(settings.registry))


def test_host_for_resolution_and_validation():
    from app.core.errors import ValidationError

    svc = _workload_service({})  # host_for doesn't touch clusters

    # no hostname -> default {name}-{group}.{route_domain}
    assert svc.host_for("app", None, "team") == "app-team.serverless.example.com"
    # single label (last octet) -> base domain appended
    assert svc.host_for("app", "shop", "team") == "shop.serverless.example.com"
    # one label under the base domain -> kept as-is
    assert (
        svc.host_for("app", "shop.serverless.example.com", "team")
        == "shop.serverless.example.com"
    )
    # FQDN outside the base domain -> rejected (surfaced as 400)
    with pytest.raises(ValidationError):
        svc.host_for("app", "shop.evil.com", "team")
    # more than one level under the base domain -> rejected
    with pytest.raises(ValidationError):
        svc.host_for("app", "a.b.serverless.example.com", "team")


async def test_host_available_when_unused():
    svc = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    # no DomainMapping exists -> no raise
    await svc.assert_host_available(
        "app-team.serverless.example.com", "app-team", svc.deployer.resolve_targets(None)
    )


async def test_host_taken_by_other_workload_conflicts():
    from app.core.errors import ConflictError
    from app.models.common import LABEL_WORKLOAD

    host = "shared.example.com"
    dm = {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "other-team"}}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={host: dm}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    with pytest.raises(ConflictError):
        await svc.assert_host_available(host, "app-team", svc.deployer.resolve_targets(None))


async def test_host_owned_by_same_workload_ok():
    from app.models.common import LABEL_WORKLOAD

    host = "app-team.serverless.example.com"
    dm = {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "app-team"}}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={host: dm}),
            "site-b": _FakeCluster("site-b", existing={host: dm}),
        }
    )
    # same owner -> update, no conflict
    await svc.assert_host_available(host, "app-team", svc.deployer.resolve_targets(None))


async def test_workload_absent_ok():
    svc = _workload_service(
        {"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")}
    )
    await svc.assert_workload_absent(
        "app", "app-team", svc.deployer.resolve_targets(None)
    )


async def test_workload_already_exists_conflicts():
    from app.core.errors import ConflictError

    ksvc = {"metadata": {"name": "app-team"}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": ksvc}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    with pytest.raises(ConflictError):
        await svc.assert_workload_absent(
            "app", "app-team", svc.deployer.resolve_targets(None)
        )


def _ksvc(offering, image="reg/x:1", group="team"):
    from app.models.common import LABEL_GROUP, LABEL_OFFERING

    return {
        "metadata": {"name": "app-team", "labels": {LABEL_GROUP: group, LABEL_OFFERING: offering}},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
    }


async def test_load_existing_returns_image():
    from app.auth.claims import Principal

    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("container")}),
            "site-b": _FakeCluster("site-b", existing={"app-team": _ksvc("container")}),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    existing = await svc.load_existing("app", "container", user, "team")
    assert existing["image"] == "reg/x:1"


async def test_load_existing_offering_mismatch_404():
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError

    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("container")}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await svc.load_existing("app", "function", user, "team")  # it's a container


async def test_accept_container_returns_pending_and_schedules():
    from fastapi import BackgroundTasks

    from app.auth.claims import Principal
    from app.models.container import ContainerCreate
    from app.services.container import ContainerService

    engine = _workload_service(
        {"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")}
    )
    svc = ContainerService(engine, engine.settings.registry)
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    spec = ContainerCreate(
        name="app", group="team", image="reg/x:1", registryUsername="u", registryToken="t"
    )
    body = await svc.accept(spec, user, bg)
    assert body.overallStatus == "Pending"
    assert body.statusUrl == "/api/v1/containers/app?group=team"
    assert len(bg.tasks) == 1  # deploy scheduled in the background


async def test_get_reports_size_and_live_usage_per_site():
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind
    from app.models.common import ANNOTATION_HOST, ANNOTATION_SIZE, LABEL_GROUP, LABEL_OFFERING

    class _UsageCluster:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return {
                    "metadata": {
                        "name": name,
                        "labels": {LABEL_GROUP: "team", LABEL_OFFERING: "container"},
                        "annotations": {
                            ANNOTATION_HOST: "app-team.serverless.example.com",
                            ANNOTATION_SIZE: "medium",
                        },
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "latestReadyRevisionName": "app-team-00001",
                    },
                }
            if kind == ResourceKind.KNATIVE_REVISION:
                assert name == "app-team-00001"
                # replicas come from here, not from the metrics pod count
                return {"status": {"actualReplicas": 3}}
            if kind == ResourceKind.POD_METRICS:
                # two replicas, each with a user container + queue-proxy sidecar
                pod = {
                    "containers": [
                        {"name": "user-container", "usage": {"cpu": "60m", "memory": "90Mi"}},
                        {"name": "queue-proxy", "usage": {"cpu": "999m", "memory": "999Mi"}},
                    ]
                }
                return [pod, pod]
            raise AssertionError(f"unexpected kind {kind}")

    engine = _workload_service({"site-a": _UsageCluster("site-a")})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get("container", "app", user, "team")
    assert body.size == "medium"
    site = body.sites[0]
    # replicas sourced from Revision.status.actualReplicas (3), not len(metrics) (2)
    assert site.replicas == 3
    # usage summed over the metrics pods' user containers, ignoring queue-proxy
    assert site.usage.cpu == "120m"
    assert site.usage.memory == "180Mi"


def _list_ksvc(oname, size, host, ready=True):
    from app.models.common import ANNOTATION_HOST, ANNOTATION_SIZE, LABEL_GROUP, LABEL_OFFERING

    return {
        "metadata": {
            "name": oname,
            "labels": {LABEL_GROUP: "team", LABEL_OFFERING: "container"},
            "annotations": {ANNOTATION_HOST: host, ANNOTATION_SIZE: size},
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "latestReadyRevisionName": "r1",
        },
    }


class _ListCluster:
    def __init__(self, name, items):
        self.site = name
        self.name = name
        self._items = items

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from app.clients.cluster import ResourceKind

        assert kind == ResourceKind.KNATIVE_SERVICE
        return list(self._items)


async def test_list_workloads_merges_sites_and_strips_suffix():
    from app.auth.claims import Principal

    a = _ListCluster("site-a", [
        _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
        _list_ksvc("web-team", "small", "web-team.ex.com"),
    ])
    b = _ListCluster("site-b", [_list_ksvc("orders-team", "medium", "orders-team.ex.com")])
    engine = _workload_service({"site-a": a, "site-b": b})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list_workloads("container", user, "team")
    # display names are sorted and have the "-{group}" suffix stripped
    assert [w.name for w in out] == ["orders", "web"]
    orders = next(w for w in out if w.name == "orders")
    assert orders.sites == ["site-a", "site-b"]  # merged across both sites
    assert orders.size == "medium"
    assert orders.url == "https://orders-team.ex.com"
    assert orders.overallStatus == "Ready"
    web = next(w for w in out if w.name == "web")
    assert web.sites == ["site-a"]  # only present on one site


async def test_list_workloads_skips_unreachable_site():
    from app.auth.claims import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    good = _ListCluster("site-a", [_list_ksvc("orders-team", "small", "orders-team.ex.com")])
    engine = _workload_service({"site-a": good, "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list_workloads("container", user, "team")
    assert [w.name for w in out] == ["orders"]  # down site skipped, not fatal


async def test_list_workloads_errors_when_all_sites_fail():
    from app.auth.claims import Principal
    from app.core.errors import SiteTotalFailure

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    engine = _workload_service({"site-a": _Boom("site-a"), "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(SiteTotalFailure):
        await engine.list_workloads("container", user, "team")


async def test_accept_rejects_group_caller_is_not_member_of():
    from fastapi import BackgroundTasks

    from app.auth.claims import Principal
    from app.core.errors import ForbiddenError
    from app.models.container import ContainerCreate
    from app.services.container import ContainerService

    engine = _workload_service({"site-a": _FakeCluster("site-a")})
    svc = ContainerService(engine, engine.settings.registry)
    user = Principal(subject="u", username="alice", groups=["team"])  # not 'other'
    spec = ContainerCreate(
        name="app", group="other", image="reg/x:1", registryUsername="u", registryToken="t"
    )
    with pytest.raises(ForbiddenError):  # 403 before anything is scheduled
        await svc.accept(spec, user, BackgroundTasks())
