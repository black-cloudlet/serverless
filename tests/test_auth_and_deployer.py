import pytest

from app.auth.claims import principal_from_claims
from app.core.config import Settings, SiteConfig, SSOConfig
from app.core.errors import SiteTotalFailure, ValidationError
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


def test_admin_key_header_carries_raw_token_not_a_hash():
    """The header must carry the RAW token, matched directly against the configured
    key. A sha256 hash of the key is not the key, so it must not authenticate."""
    import hashlib
    from types import SimpleNamespace

    from app.auth.deps import require_auth
    from app.core.errors import UnauthenticatedError

    settings = _settings_with_admin_key("opaque-s3cret")
    hashed = hashlib.sha256(b"opaque-s3cret").hexdigest()
    req = SimpleNamespace(headers={"Authorization": f"Bearer {hashed}"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req, settings, validator=None)


def test_empty_admin_key_disables_key_auth():
    """Setting the admin key empty disables API-key auth entirely; no opaque token
    (including the empty string) is accepted."""
    from types import SimpleNamespace

    from app.auth.deps import require_auth
    from app.core.errors import UnauthenticatedError

    settings = _settings_with_admin_key("")
    req = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req, settings, validator=None)


def test_verify_admin_key_constant_time_match():
    from app.auth.apikey import verify_admin_key

    assert verify_admin_key("opaque-s3cret", "opaque-s3cret") is True
    assert verify_admin_key("wrong", "opaque-s3cret") is False
    assert verify_admin_key("", "opaque-s3cret") is False
    assert verify_admin_key("anything", "") is False


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
    assert aggregate(statuses) == "Ready"
    assert status_code_for("Ready", created=True) == 201


def test_aggregate_still_rolling_out():
    # a just-applied workload (sites not Ready yet) reports Deploying, not Ready
    statuses = [SiteStatus(site="a", status="Ready"), SiteStatus(site="b", status="Deploying")]
    assert aggregate(statuses) == "Deploying"


def test_aggregate_partial():
    statuses = [
        SiteStatus(site="a", status="Ready"),
        SiteStatus(site="b", status="Failed", error="boom"),  # unreachable -> Failed
    ]
    assert aggregate(statuses) == "Degraded"
    assert status_code_for("Degraded", created=True) == 207


def test_aggregate_total_failure():
    statuses = [SiteStatus(site="a", status="Failed", error="x")]
    with pytest.raises(SiteTotalFailure):
        aggregate(statuses)


def test_overall_status_rollup():
    from app.services.deployer import overall_status

    assert overall_status(["Ready", "Ready"]) == "Ready"
    assert overall_status(["Deploying", "Deploying"]) == "Deploying"
    # a normal rollout where one site is ahead is still in-progress, not Degraded
    assert overall_status(["Ready", "Deploying"]) == "Deploying"
    # any failed (or unreachable, mapped to Failed) site -> Degraded
    assert overall_status(["Ready", "Failed"]) == "Degraded"
    assert overall_status(["Deploying", "Failed"]) == "Degraded"
    assert overall_status([]) == "Degraded"


def test_status_code_for_deploying_is_non_terminal():
    # Deploying is an accepted, still-rolling-out state, not a partial failure
    assert status_code_for("Deploying", created=True) == 202
    assert status_code_for("Deploying", created=False) == 202
    assert status_code_for("Ready", created=False) == 200


def test_ksvc_status_distinguishes_failed_from_deploying():
    from app.services.workloads import _ksvc_status

    def _obj(ready_status):
        conditions = [{"type": "Ready", "status": ready_status}] if ready_status else []
        return {"status": {"conditions": conditions, "latestCreatedRevisionName": "rev-1"}}

    assert _ksvc_status(_obj("True")) == ("Ready", "rev-1")
    assert _ksvc_status(_obj("False")) == ("Failed", "rev-1")  # terminal failure
    assert _ksvc_status(_obj("Unknown")) == ("Deploying", "rev-1")  # still progressing
    assert _ksvc_status(_obj(None)) == ("Deploying", "rev-1")  # no condition yet
    assert _ksvc_status({}) == ("Deploying", None)  # brand-new, no status block


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


def _bare_ksvc(name="app-team"):
    from app.models.common import Scaling
    from app.services.ksvc import build_ksvc

    return build_ksvc(
        name=name, group="team", owner="alice", image="reg/app:1",
        offering="container", host=f"{name}.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
    )


async def test_get_overall_status_reflects_rollout_state():
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind

    ksvc = _bare_ksvc()

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            raise RuntimeError("replicas/usage/spec extras are best-effort here")

    engine = _workload_service({"site-a": _C()})
    user = Principal(subject="u", username="alice", groups=["team"])

    async def _overall():
        return (await engine.get("container", "app", user, "team")).overallStatus

    # no Ready condition yet -> Deploying (regression: this used to be Degraded)
    assert await _overall() == "Deploying"
    # Ready=True -> Ready
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    assert await _overall() == "Ready"
    # Ready=False -> terminal failure surfaces as Degraded
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}
    assert await _overall() == "Degraded"


async def test_list_overall_status_per_workload():
    from app.auth.claims import Principal

    deploying = _bare_ksvc("app-team")  # no conditions -> Deploying
    failed = _bare_ksvc("bad-team")
    failed["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}

    engine = _workload_service(
        {"site-a": _ListCluster("site-a", [deploying, failed])}, local_site="site-a"
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    summaries = {s.name: s.overallStatus for s in await engine.list("container", user, "team")}

    assert summaries["app"] == "Deploying"  # not a false Degraded
    assert summaries["bad"] == "Degraded"


class _ApplyCluster:
    """Records applied manifests; serves a preset existing KSVC."""

    def __init__(self, name, existing):
        self.site = name
        self.name = name
        self._existing = existing  # oname -> ksvc dict
        self.applied = []
        self.deleted = []  # [(ResourceKind, name)] from prune

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from app.clients.cluster import ResourceKind
        from app.core.errors import NotFoundError as _NF

        if kind == ResourceKind.KNATIVE_SERVICE and name in self._existing:
            return self._existing[name]
        raise _NF("not found")  # domain mapping -> Available; missing ksvc

    def apply(self, manifest):
        self.applied.append(manifest)
        # Mirror the real client: return the applied object(s), with a uid the
        # server would assign (used to build ownerReferences for derived objects).
        meta = manifest.get("metadata", {}) or {}
        applied = {**manifest, "metadata": {**meta, "uid": f"uid-{meta.get('name')}"}}
        if manifest.get("kind") == "Service":  # now readable back by get()
            self._existing[meta.get("name")] = applied
        return [applied]

    def delete(self, kind, name):
        self.deleted.append((kind, name))


def _applied_kind(cluster, kind):
    return [m for m in cluster.applied if m.get("kind") == kind]


async def test_update_prunes_backing_no_longer_referenced():
    """Dropping the last secret env var / secret file on update must remove the
    now-stale {workload}-env / {workload}-files objects, while a still-referenced
    files ConfigMap is kept."""
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind
    from app.models.common import EnvVar, FileMount, Scaling
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService
    from app.services.ksvc import build_ksvc

    existing = build_ksvc(
        name="api-team", group="team", owner="alice", image="reg/app:1",
        offering="container", host="api-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # New spec keeps only a PLAIN env var and a NON-secret file -> no env Secret,
    # a files ConfigMap, but no files Secret.
    await csvc.update(
        "api",
        ContainerUpdate(
            group="team",
            env=[EnvVar(name="LOG", value="debug")],          # plain -> no env Secret
            files=[FileMount(mountPath="/etc/app.conf", content="x")],  # -> files ConfigMap
        ),
        user,
    )

    deleted = set(cluster.deleted)
    assert (ResourceKind.SECRET, "api-team-env") in deleted     # no secret env -> pruned
    assert (ResourceKind.SECRET, "api-team-files") in deleted   # no secret files -> pruned
    # the files ConfigMap is still referenced -> applied, never pruned
    assert (ResourceKind.CONFIG_MAP, "api-team-files") not in deleted
    assert any(m["kind"] == "ConfigMap" and m["metadata"]["name"] == "api-team-files"
               for m in cluster.applied)


async def test_create_does_not_prune():
    """On create there is nothing to prune; no deletes are issued."""
    from app.auth.claims import Principal
    from app.services.container import ContainerService

    cluster = _ApplyCluster("site-a", {})  # nothing exists yet
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    from app.models.container import ContainerCreate
    await csvc.create(
        ContainerCreate(name="api", group="team", image="reg/x:1"), user
    )
    assert cluster.deleted == []


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
    import base64 as _b64
    import json as _json
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


async def test_list_fans_out_and_merges_sites():
    from app.auth.claims import Principal

    # orders is deployed to both sites; web only to site-a.
    site_a = _ListCluster("site-a", [
        _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
        _list_ksvc("web-team", "small", "web-team.ex.com"),
    ])
    site_b = _ListCluster("site-b", [
        _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
    ])
    engine = _workload_service({"site-a": site_a, "site-b": site_b})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list("container", user, "team")
    assert [w.name for w in out] == ["orders", "web"]  # sorted, suffix stripped

    orders = next(w for w in out if w.name == "orders")
    assert orders.sites == ["site-a", "site-b"]  # merged across both sites
    assert orders.size == "medium"
    assert orders.hostname == "orders-team.ex.com"
    assert orders.overallStatus == "Ready"

    web = next(w for w in out if w.name == "web")
    # single-site workload: only the site that has it, status over just that site
    assert web.sites == ["site-a"]
    assert web.overallStatus == "Ready"  # not a false Degraded for the absent site


async def test_list_skips_unreachable_site_best_effort():
    from app.auth.claims import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    # site-a answers, site-b is down -> merge what site-a returned, don't fail.
    site_a = _ListCluster("site-a", [
        _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
    ])
    engine = _workload_service({"site-a": site_a, "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list("container", user, "team")
    assert [w.name for w in out] == ["orders"]
    assert out[0].sites == ["site-a"]  # only the reachable site contributes


async def test_list_sort_by_created_at():
    from app.auth.claims import Principal

    def _with_created(oname, created):
        k = _list_ksvc(oname, "small", f"{oname}.ex.com")
        k["metadata"]["creationTimestamp"] = created
        return k

    local = _ListCluster("site-a", [
        _with_created("aaa-team", "2026-01-02T00:00:00Z"),  # name first, created newer
        _with_created("bbb-team", "2026-01-01T00:00:00Z"),  # name last, created older
    ])
    engine = _workload_service({"site-a": local}, local_site="site-a")
    user = Principal(subject="u", username="alice", groups=["team"])

    by_name = await engine.list("container", user, "team")  # default
    assert [w.name for w in by_name] == ["aaa", "bbb"]
    assert by_name[0].createdAt is not None  # creation time populated

    by_created = await engine.list("container", user, "team", sort="createdAt")
    assert [w.name for w in by_created] == ["bbb", "aaa"]  # oldest first


async def test_list_errors_when_all_sites_fail():
    from app.auth.claims import Principal
    from app.core.errors import SiteTotalFailure

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    # Only when *every* site is unreachable is the list failed.
    engine = _workload_service({"site-a": _Boom("site-a"), "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(SiteTotalFailure):
        await engine.list("container", user, "team")


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


class _DeleteCluster:
    """Records deletions; serves a preset KSVC (or none) by name."""

    def __init__(self, name, ksvc):
        self.name = name
        self.site = name
        self._ksvc = ksvc          # the KSVC dict, or None if absent
        self.deleted = []          # [(ResourceKind, name)]

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from app.clients.cluster import ResourceKind
        from app.core.errors import NotFoundError as _NF

        if kind == ResourceKind.KNATIVE_SERVICE and self._ksvc is not None:
            return self._ksvc
        raise _NF(f"{name} not found")

    def delete(self, kind, name):
        self.deleted.append((kind, name))


async def test_delete_removes_ksvc_and_relies_on_gc():
    """Delete removes only the KSVC; the derived resources are garbage-collected
    by Kubernetes via their ownerReferences (set at apply time — see
    test_apply_sets_owner_references_on_derived)."""
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind

    cluster = _DeleteCluster("site-a", _ksvc("container"))
    engine = _workload_service({"site-a": cluster})
    user = Principal(subject="u", username="alice", groups=["team"])

    await engine.delete("container", "app", user, "team")

    assert cluster.deleted == [(ResourceKind.KNATIVE_SERVICE, "app-team")]


async def test_delete_missing_workload_is_404():
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError

    cluster = _DeleteCluster("site-a", None)  # no KSVC present
    engine = _workload_service({"site-a": cluster})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await engine.delete("container", "app", user, "team")
    assert cluster.deleted == []  # nothing deleted when the workload is absent


async def test_apply_sets_owner_references_on_derived():
    """Every resource derived from a workload (env/files Secret & ConfigMap, the
    imagePullSecret, and the DomainMapping) must carry an ownerReference to the
    KSVC so Kubernetes GC cleans them up on delete. The KSVC itself owns nothing."""
    from app.auth.claims import Principal
    from app.models.common import EnvVar, FileMount, Scaling
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService
    from app.services.ksvc import build_ksvc

    existing = build_ksvc(
        name="api-team", group="team", owner="alice", image="reg.acme.com/api:1",
        offering="container", host="api-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.update(
        "api",
        ContainerUpdate(
            group="team",
            registryUsername="bob", registryToken="t",   # -> pull Secret
            env=[EnvVar(name="PW", value="x", secret=True)],  # -> env Secret
            files=[FileMount(mountPath="/etc/app/c", content="hi")],  # -> files ConfigMap
        ),
        user,
    )

    ksvc = _applied_kind(cluster, "Service")[0]
    assert "ownerReferences" not in ksvc["metadata"]  # the owner owns nothing

    derived = [m for m in cluster.applied if m.get("kind") in ("Secret", "ConfigMap", "DomainMapping")]
    assert derived, "expected derived resources to be applied"
    for m in derived:
        refs = m["metadata"].get("ownerReferences")
        assert refs, f"{m['kind']}/{m['metadata']['name']} missing ownerReference"
        ref = refs[0]
        assert ref["kind"] == "Service"
        assert ref["name"] == "api-team"
        assert ref["uid"] == "uid-api-team"
        assert ref["blockOwnerDeletion"] is False


class _DownCluster:
    """A site that is unreachable: every read fails with a non-404 error."""

    def __init__(self, name="site-a"):
        self.site = name
        self.name = name

    def get(self, *a, **k):
        raise RuntimeError("connection refused")  # not a NotFoundError


async def test_host_check_fails_closed_when_site_unreachable():
    # An unreachable site can't prove the host is free -> 503, not a silent pass.
    from app.core.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})
    with pytest.raises(ServiceUnavailableError):
        await svc.assert_host_available(
            "h.example.com", "app-team", svc.deployer.resolve_targets(None)
        )


async def test_absent_check_fails_closed_when_site_unreachable():
    from app.core.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})
    with pytest.raises(ServiceUnavailableError):
        await svc.assert_workload_absent(
            "app", "app-team", svc.deployer.resolve_targets(None)
        )


async def test_host_check_still_available_on_real_404():
    # A genuine NotFound (404) still reads as available across a reachable site.
    svc = _workload_service({"site-a": _FakeCluster("site-a")})  # get -> NotFoundError
    await svc.assert_host_available(
        "h.example.com", "app-team", svc.deployer.resolve_targets(None)
    )


async def test_accept_rejects_invalid_spec_synchronously():
    # A malformed spec must 400 at accept time, before anything is scheduled.
    from fastapi import BackgroundTasks

    from app.auth.claims import Principal
    from app.core.errors import ValidationError
    from app.models.common import FileMount
    from app.models.container import ContainerCreate
    from app.services.container import ContainerService

    engine = _workload_service({"site-a": _DownCluster()})  # would error if reached
    svc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    spec = ContainerCreate(
        name="app", group="team", image="reg/x:1",
        files=[FileMount(mountPath="/a/conf", content="1"),
               FileMount(mountPath="/a/conf", content="2")],  # duplicate key
    )
    with pytest.raises(ValidationError):
        await svc.accept(spec, user, bg)
    assert bg.tasks == []  # validation ran before the background deploy was queued


async def test_update_reuses_existing_without_refetch():
    # accept_update passes the loaded `existing` into update(); update must reuse
    # it and NOT trigger a second load_existing fanout.
    from app.auth.claims import Principal
    from app.models.common import Scaling
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService
    from app.services.ksvc import build_ksvc

    existing_ksvc = build_ksvc(
        name="api-team", group="team", owner="alice", image="reg/app:1",
        offering="container", host="api-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
    )
    engine = _workload_service({"site-a": _ApplyCluster("site-a", {"api-team": existing_ksvc})})

    calls = {"n": 0}
    original = engine.load_existing

    async def counting(*a, **k):
        calls["n"] += 1
        return await original(*a, **k)

    engine.load_existing = counting
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])
    preloaded = {"image": "reg/app:1", "pull_secret": None}
    await csvc.update("api", ContainerUpdate(group="team"), user, existing=preloaded)
    assert calls["n"] == 0  # reused the passed-in existing, no second fanout


async def test_load_existing_unreachable_site_is_503_not_404():
    # A workload that can't be confirmed absent because the site is down must
    # surface ServiceUnavailable, not a misleading NotFound.
    from app.auth.claims import Principal
    from app.core.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})  # get() raises RuntimeError
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(ServiceUnavailableError):
        await svc.load_existing("app", "container", user, "team")


async def test_load_existing_truly_absent_is_404():
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError

    svc = _workload_service({"site-a": _FakeCluster("site-a")})  # get() -> NotFoundError
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await svc.load_existing("app", "container", user, "team")


def _ready_ksvc(name="app-team"):
    k = _bare_ksvc(name)
    k["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    return k


async def test_get_omits_404_site_and_stays_healthy():
    # A site that returns a clean 404 (workload not deployed there) is omitted from
    # the per-site report and does NOT drag the rollup to Degraded.
    from app.auth.claims import Principal

    engine = _workload_service({
        "site-a": _FakeCluster("site-a", existing={"app-team": _ready_ksvc()}),
        "site-b": _FakeCluster("site-b"),  # 404 here
    })
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get("container", "app", user, "team")
    assert body.overallStatus == "Ready"
    assert [s.site for s in body.sites] == ["site-a"]  # site-b omitted, not Failed


async def test_get_down_site_still_degrades():
    # An UNREACHABLE site is different from a 404: it stays visible and degrades.
    from app.auth.claims import Principal

    engine = _workload_service({
        "site-a": _FakeCluster("site-a", existing={"app-team": _ready_ksvc()}),
        "site-b": _DownCluster("site-b"),  # unreachable
    })
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get("container", "app", user, "team")
    assert body.overallStatus == "Degraded"
    assert {s.site for s in body.sites} == {"site-a", "site-b"}


async def test_get_absent_everywhere_is_404():
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError

    engine = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await engine.get("container", "app", user, "team")


async def test_get_all_sites_down_is_503():
    # Can't confirm absence when every site is unreachable -> 503, not a false 404.
    from app.auth.claims import Principal
    from app.core.errors import ServiceUnavailableError

    engine = _workload_service({"site-a": _DownCluster("site-a"), "site-b": _DownCluster("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(ServiceUnavailableError):
        await engine.get("container", "app", user, "team")


def test_principal_normalizes_slash_and_ggd_prefixes():
    from app.auth.claims import principal_from_claims
    from app.core.config import SSOConfig

    cfg = SSOConfig(groups_claim="groups", admin_groups=["platform-admins"])
    p = principal_from_claims(
        {"sub": "u", "groups": ["/ggd-1234-team-a", "ggd-7-platform-admins", "/plain"]},
        cfg,
    )
    assert p.groups == ["team-a", "platform-admins", "plain"]  # / and ggd-NNNN stripped
    assert p.is_admin is True  # matched after normalization


def test_validate_group_strips_ggd_prefix_on_input():
    # A request-supplied group with the ggd- prefix normalizes to the bare name,
    # so "ggd-1234-platforms" and "platforms" are the same group.
    from app.models.common import normalize_group, validate_group

    assert validate_group("ggd-1234-platforms") == "platforms"
    assert validate_group("platforms") == "platforms"
    assert normalize_group("/ggd-7-team-a") == "team-a"
    assert normalize_group("ggd-1234platforms") == "ggd-1234platforms"  # no dash -> kept


def test_sso_endpoints_derived_from_issuer():
    from app.core.config import SSOConfig

    cfg = SSOConfig(issuer="https://sso.x/realms/r")
    assert cfg.discovery_url == "https://sso.x/realms/r/.well-known/openid-configuration"
    assert cfg.authorization_url == "https://sso.x/realms/r/protocol/openid-connect/auth"
    assert cfg.token_url == "https://sso.x/realms/r/protocol/openid-connect/token"
    assert cfg.audience == ""  # empty by default -> audience not verified


def test_validate_audience_verified_only_when_configured(monkeypatch):
    import app.auth.oidc as oidc_mod
    from app.auth.oidc import TokenValidator
    from app.core.config import SSOConfig

    captured: dict = {}

    class _Key:
        key = "k"

    class _Client:
        def get_signing_key_from_jwt(self, _t):
            return _Key()

    def fake_decode(_token, _key, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"sub": "u"}

    monkeypatch.setattr(oidc_mod.jwt, "decode", fake_decode)

    # No audience configured -> aud not checked.
    none = TokenValidator(SSOConfig(audience=""))
    monkeypatch.setattr(none, "_client", lambda: _Client())
    none.validate("tok")
    assert captured["audience"] is None
    assert captured["options"]["verify_aud"] is False

    # Audience configured -> aud enforced.
    on = TokenValidator(SSOConfig(audience="serverless-api"))
    monkeypatch.setattr(on, "_client", lambda: _Client())
    on.validate("tok")
    assert captured["audience"] == "serverless-api"
    assert "verify_aud" not in captured["options"]


# --- Review fixes: fail-closed prune (A1/A3), client cleanup (B1), guards (A2, D1) ---


def _existing_container_ksvc():
    from app.models.common import Scaling
    from app.services.ksvc import build_ksvc

    return build_ksvc(
        name="api-team", group="team", owner="alice", image="reg/app:1",
        offering="container", host="api-team.ex.com", env=[], volumes=[],
        scaling=Scaling(), size="small",
    )


async def test_prune_failure_aborts_update_fail_closed():
    """A non-404 prune error must abort the site's update: the new spec never goes
    live, so it can't sit alongside the stale, now-unreferenced secret."""
    from app.auth.claims import Principal
    from app.models.common import EnvVar
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService

    class _PruneFails(_ApplyCluster):
        def delete(self, kind, name):  # a real API error, not a 404
            raise RuntimeError("boom: API error, not a 404")

    cluster = _PruneFails("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):  # single site failed -> total failure
        await csvc.update(
            "api",
            ContainerUpdate(group="team", env=[EnvVar(name="LOG", value="debug")]),
            user,
        )
    # fail-closed: the KSVC was never (re)applied after the prune failed
    assert _applied_kind(cluster, "Service") == []


async def test_prune_not_found_is_tolerated():
    """A 404 during prune just means the object never existed here; the update
    proceeds normally."""
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError
    from app.models.common import EnvVar
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService

    class _PruneMissing(_ApplyCluster):
        def delete(self, kind, name):
            self.deleted.append((kind, name))
            raise NotFoundError("already gone")

    cluster = _PruneMissing("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.update(  # must not raise
        "api",
        ContainerUpdate(group="team", env=[EnvVar(name="LOG", value="debug")]),
        user,
    )
    assert _applied_kind(cluster, "Service")  # KSVC applied -> update proceeded


async def test_prune_runs_before_apply_on_update():
    """Fail-closed ordering: every prune happens before the new spec is applied."""
    from app.auth.claims import Principal
    from app.models.common import EnvVar
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService

    class _OpLog(_ApplyCluster):
        def __init__(self, name, existing):
            super().__init__(name, existing)
            self.ops = []

        def apply(self, manifest):
            self.ops.append("apply")
            return super().apply(manifest)

        def delete(self, kind, name):
            self.ops.append("delete")
            return super().delete(kind, name)

    cluster = _OpLog("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.update(
        "api",
        ContainerUpdate(group="team", env=[EnvVar(name="LOG", value="debug")]),
        user,
    )
    first_apply = cluster.ops.index("apply")
    last_delete = max(i for i, op in enumerate(cluster.ops) if op == "delete")
    assert last_delete < first_apply  # all prunes precede the first apply


async def test_list_total_failure_details_have_message_key():
    """The list total-failure details share deployer.aggregate's {site, message}
    shape so clients parse one envelope."""
    from app.auth.claims import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    engine = _workload_service({"site-a": _Boom("site-a"), "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(SiteTotalFailure) as ei:
        await engine.list("container", user, "team")
    assert all(set(d) == {"site", "message"} for d in ei.value.details)


def test_looks_like_jwt_rejects_non_string_and_empty():
    """A None/empty/non-str token returns False (a clean 401 path) instead of
    raising a 500 from the JWT parser."""
    from app.auth.oidc import looks_like_jwt

    assert looks_like_jwt(None) is False
    assert looks_like_jwt("") is False
    assert looks_like_jwt(123) is False  # type: ignore[arg-type]
    assert looks_like_jwt("not-a-jwt") is False


def test_deployer_close_releases_every_cluster():
    closed = []

    class _C:
        def __init__(self, name):
            self.name = name
            self.site = name

        def close(self):
            closed.append(self.site)

    d = Deployer(_settings_with_sites())
    d._clusters = {"a": _C("a"), "b": _C("b")}
    d.close()
    assert sorted(closed) == ["a", "b"]


def test_cluster_close_closes_api_client_and_resets():
    from app.clients.cluster import Cluster

    settings = _settings_with_sites()
    c = Cluster(settings.sites[0], settings)
    calls = {"n": 0}

    class _Api:
        def close(self):
            calls["n"] += 1

    c._api_client_obj = _Api()
    c._dynamic_client_obj = object()
    c.close()
    assert calls["n"] == 1
    assert c._api_client_obj is None and c._dynamic_client_obj is None
    c.close()  # idempotent: no client to close now
    assert calls["n"] == 1


# --- Terminating status (#3) and Israel-local timestamps (#2) ---


async def test_get_reports_terminating_during_delete():
    """A KSVC carrying a deletionTimestamp (being garbage-collected) reports
    Terminating rather than a stale Ready."""
    from app.auth.claims import Principal

    terminating = _ready_ksvc()  # would otherwise read Ready
    terminating["metadata"]["deletionTimestamp"] = "2026-06-29T12:00:00Z"
    engine = _workload_service({
        "site-a": _FakeCluster("site-a", existing={"app-team": terminating}),
        "site-b": _FakeCluster("site-b"),  # 404 here
    })
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get("container", "app", user, "team")
    assert body.overallStatus == "Terminating"
    assert body.sites[0].status == "Terminating"


def test_overall_status_terminating_precedence():
    from app.services.deployer import overall_status

    assert overall_status(["Terminating", "Ready"]) == "Terminating"
    assert overall_status(["Terminating", "Deploying"]) == "Terminating"
    # A real failure still outranks a termination in progress.
    assert overall_status(["Failed", "Terminating"]) == "Degraded"


def test_creation_time_is_israel_local_time_with_dst():
    """metadata.creationTimestamp (UTC) is surfaced in Israel local time, with the
    DST-aware offset: +03:00 (IDT) in summer, +02:00 (IST) in winter."""
    from zoneinfo import ZoneInfo

    from app.services.workloads import _creation_time

    summer = _creation_time({"metadata": {"creationTimestamp": "2026-07-01T09:00:00Z"}})
    assert summer.tzinfo == ZoneInfo("Asia/Jerusalem")
    assert summer.utcoffset().total_seconds() == 3 * 3600  # IDT
    assert (summer.hour, summer.minute) == (12, 0)  # 09:00Z -> 12:00 IDT

    winter = _creation_time({"metadata": {"creationTimestamp": "2026-01-01T09:00:00Z"}})
    assert winter.utcoffset().total_seconds() == 2 * 3600  # IST
    assert winter.hour == 11  # 09:00Z -> 11:00 IST

    assert _creation_time({"metadata": {}}) is None  # absent -> None


# --- A1: roll back a failed create, never a failed update ---


class _BackingFails(_ApplyCluster):
    """Applies the KSVC fine, but fails on any derived backing/mapping apply."""

    def apply(self, manifest):
        if manifest.get("kind") == "Service":
            return super().apply(manifest)
        raise RuntimeError("backing apply failed")


async def test_create_rolls_back_ksvc_on_backing_failure():
    """If a backing/mapping apply fails mid-create, the just-created KSVC is
    deleted (rolled back) so no half-built workload lingers on the name/host."""
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind
    from app.models.container import ContainerCreate
    from app.services.container import ContainerService

    cluster = _BackingFails("site-a", {})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):  # single site failed
        await csvc.create(ContainerCreate(name="api", group="team", image="reg/x:1"), user)
    assert (ResourceKind.KNATIVE_SERVICE, "api-team") in cluster.deleted


async def test_update_does_not_roll_back_live_ksvc_on_backing_failure():
    """A backing/mapping failure on update must NOT delete the live KSVC — Knative
    keeps serving the last-good revision; deleting would take it down + free the host."""
    from app.auth.claims import Principal
    from app.clients.cluster import ResourceKind
    from app.models.common import EnvVar
    from app.models.container import ContainerUpdate
    from app.services.container import ContainerService

    cluster = _BackingFails("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):
        await csvc.update(
            "api",
            ContainerUpdate(group="team", env=[EnvVar(name="LOG", value="debug")]),
            user,
        )
    assert (ResourceKind.KNATIVE_SERVICE, "api-team") not in cluster.deleted
