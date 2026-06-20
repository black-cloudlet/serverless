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


def test_local_cluster_selection():
    d = Deployer(_settings_with_sites())
    # unset -> first configured site
    assert d.local_cluster().site == "site-a"
    # match by site name
    d._local_site = "site-b"
    assert d.local_cluster().site == "site-b"
    # match by cluster name (Cluster.name), not just site name
    d._local_site = "site-b-0"
    assert d.local_cluster().site == "site-b"
    # unknown value -> deterministic fallback to the first site
    d._local_site = "nope"
    assert d.local_cluster().site == "site-a"


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


def _workload_service(clusters, builder=None, local_site=None):
    from app.services.builder import FuncBuilder
    from app.services.workloads import WorkloadService

    settings = _settings_with_sites()
    d = Deployer(settings)
    d._clusters = clusters  # inject fakes (name -> _FakeCluster)
    d._local_site = local_site
    return WorkloadService(settings, d, builder or FuncBuilder(settings.registry))


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
    svc = ContainerService(engine)
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


async def test_get_returns_redacted_spec():
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind
    from app.models.common import Scaling
    from app.services.files import VolumeSpec
    from app.services.ksvc import ContainerEnv, build_ksvc

    ksvc = build_ksvc(
        name="app-team", group="team", owner="alice",
        image="reg/app:1", offering="container", host="app-team.ex.com",
        env=[
            ContainerEnv(name="LOG", value="debug"),
            ContainerEnv(name="API_KEY", secret_ref=("app-team-env", "API_KEY")),
        ],
        volumes=[
            VolumeSpec("files-config", "configmap", "app-team-files", "/etc/app.conf", "etc-app.conf", True),
            VolumeSpec("files-secret", "secret", "app-team-files", "/etc/secret", "etc-secret", True),
        ],
        scaling=Scaling(minScale=1, maxScale=4, metric="cpu", target=80),
        size="medium", pull_secret="app-team-pull",
        ca_config_map="trusted-ca", ca_mount_path="/etc/pki/tls/certs/ca.crt",
    )

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            from app.services.secrets import build_pull_secret

            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            if kind == ResourceKind.CONFIG_MAP:
                assert name == "app-team-files"
                return {"data": {"etc-app.conf": "level=debug"}}
            if kind == ResourceKind.SECRET:
                assert name == "app-team-pull"
                return build_pull_secret(name, {}, "reg.example.com", "bob", "s3cr3t")
            raise RuntimeError("revision/metrics are best-effort here")

    engine = _workload_service({"site-a": _C()})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get("container", "app", user, "team")

    # flattened ContainerResponse mirrors the create body (secrets redacted)
    assert body.image == "reg/app:1"  # container image returned on read
    assert body.scaling.metric == "cpu" and body.scaling.effective_target == 80
    envs = {e.name: e for e in body.env}
    assert envs["LOG"].value == "debug" and envs["LOG"].secret is False
    assert envs["API_KEY"].secret is True and envs["API_KEY"].value is None
    files = {f.mountPath: f for f in body.files}
    assert files["/etc/app.conf"].content == "level=debug"  # plain content
    assert files["/etc/secret"].secret is True and files["/etc/secret"].content is None
    # registry username shown, token never returned
    assert body.registryUsername == "bob"


class _ApplyCluster:
    """Records applied manifests; serves a preset existing KSVC."""

    def __init__(self, name, existing):
        self.site = name
        self.name = name
        self._existing = existing  # oname -> ksvc dict
        self.applied = []

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from app.clients.cluster import ResourceKind
        from app.core.errors import NotFoundError as _NF

        if kind == ResourceKind.KNATIVE_SERVICE and name in self._existing:
            return self._existing[name]
        raise _NF("not found")  # domain mapping -> Available; missing ksvc

    def apply(self, manifest):
        self.applied.append(manifest)


def _applied_kind(cluster, kind):
    return [m for m in cluster.applied if m.get("kind") == kind]


async def test_container_update_rotates_pull_secret():
    from app.auth.claims import Principal
    from app.models.common import Scaling
    from app.services.container import ContainerService
    from app.services.ksvc import build_ksvc

    existing = build_ksvc(
        name="api-team", group="team", owner="alice", image="reg.acme.com/api:1",
        offering="container", host="api-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",  # public image: no pull secret
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    from app.models.container import ContainerUpdate
    await csvc.update("api", ContainerUpdate(group="team", registryUsername="bob", registryToken="t"), user)

    secrets = [m for m in _applied_kind(cluster, "Secret")
               if m.get("type") == "kubernetes.io/dockerconfigjson"]
    assert secrets and secrets[0]["metadata"]["name"] == "api-team-pull"
    # the pull secret is keyed to the client image's registry, not our platform one
    import base64 as _b64, json as _json
    cfg = _json.loads(_b64.b64decode(secrets[0]["data"][".dockerconfigjson"]))
    assert set(cfg["auths"]) == {"reg.acme.com"}
    ksvc = _applied_kind(cluster, "Service")[0]
    assert ksvc["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "api-team-pull"}]


async def test_function_update_rebuilds_when_token_given():
    from app.auth.claims import Principal
    from app.models.common import Scaling
    from app.models.function import FunctionUpdate
    from app.services.builder import BuildResult
    from app.services.function import FunctionService
    from app.services.ksvc import build_ksvc
    from app.services.workloads import _extract_image

    class _StubBuilder:
        def __init__(self):
            self.calls = 0

        def build(self, req):
            self.calls += 1
            self.req = req
            return BuildResult(image="reg/built:rel", digest="sha256:abc")

    existing = build_ksvc(
        name="fn-team", group="team", owner="alice", image="reg/fn:old",
        offering="function", host="fn-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
        runtime="python", git_url="https://git/old.git", branch="main",
    )
    cluster = _ApplyCluster("site-a", {"fn-team": existing})
    builder = _StubBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    fsvc = FunctionService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # rebuild from a new branch; gitRepo/runtime carried from existing
    await fsvc.update("fn", FunctionUpdate(group="team", branch="release", gitToken="tok"), user)
    assert builder.calls == 1
    assert builder.req.branch == "release"
    assert builder.req.git_url == "https://git/old.git"
    assert builder.req.runtime == "python"
    ksvc = _applied_kind(cluster, "Service")[0]
    assert _extract_image(ksvc) == "sha256:abc"  # rebuilt digest deployed


async def test_function_update_without_token_keeps_image():
    from app.auth.claims import Principal
    from app.models.common import Scaling
    from app.models.function import FunctionUpdate
    from app.services.function import FunctionService
    from app.services.ksvc import build_ksvc
    from app.services.workloads import _extract_image

    class _StubBuilder:
        def __init__(self):
            self.calls = 0

        def build(self, req):
            self.calls += 1
            raise AssertionError("must not rebuild for a config-only update")

    existing = build_ksvc(
        name="fn-team", group="team", owner="alice", image="reg/fn:old",
        offering="function", host="fn-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small", runtime="python",
        git_url="https://git/old.git", branch="main",
    )
    cluster = _ApplyCluster("site-a", {"fn-team": existing})
    builder = _StubBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    fsvc = FunctionService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    await fsvc.update("fn", FunctionUpdate(group="team", scaling=Scaling(minScale=2, maxScale=2)), user)
    assert builder.calls == 0
    ksvc = _applied_kind(cluster, "Service")[0]
    assert _extract_image(ksvc) == "reg/fn:old"  # existing image preserved


async def test_list_workloads_reads_only_local_site():
    from app.auth.claims import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise AssertionError("remote site must not be queried for a list")

    local = _ListCluster("site-a", [
        _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
        _list_ksvc("web-team", "small", "web-team.ex.com"),
    ])
    # local_site=site-a -> only site-a is read; site-b would raise if touched
    engine = _workload_service(
        {"site-a": local, "site-b": _Boom("site-b")}, local_site="site-a"
    )
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list_workloads("container", user, "team")
    assert [w.name for w in out] == ["orders", "web"]  # sorted, suffix stripped
    orders = next(w for w in out if w.name == "orders")
    assert orders.sites == ["site-a"]  # only the local site is reported
    assert orders.size == "medium"
    assert orders.hostname == "orders-team.ex.com"
    assert orders.overallStatus == "Ready"


async def test_list_workloads_errors_when_local_site_fails():
    from app.auth.claims import Principal
    from app.core.errors import SiteTotalFailure

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    engine = _workload_service({"site-a": _Boom("site-a")}, local_site="site-a")
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
    svc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])  # not 'other'
    spec = ContainerCreate(
        name="app", group="other", image="reg/x:1", registryUsername="u", registryToken="t"
    )
    with pytest.raises(ForbiddenError):  # 403 before anything is scheduled
        await svc.accept(spec, user, BackgroundTasks())
