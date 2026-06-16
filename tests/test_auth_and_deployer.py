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


def _settings_with_api_key(raw_key, groups, admin_groups=()):
    import hashlib

    from app.core.config import ApiKey

    return Settings(
        auth_enabled=True,
        sso=SSOConfig(admin_groups=list(admin_groups)),
        api_keys=[
            ApiKey(
                name="ci-bot",
                sha256=hashlib.sha256(raw_key.encode()).hexdigest(),
                groups=groups,
            )
        ],
    )


def test_authenticate_api_key_is_admin():
    from app.auth.apikey import authenticate_api_key

    settings = _settings_with_api_key("s3cret", ["platform-admins"], ["platform-admins"])
    p = authenticate_api_key("s3cret", settings)
    assert p.username == "ci-bot"
    assert p.is_admin is True  # API keys are admin-only
    assert authenticate_api_key("wrong", settings) is None


def test_require_auth_via_bearer_api_key():
    from types import SimpleNamespace

    from app.auth.deps import require_auth
    from app.core.errors import UnauthenticatedError

    settings = _settings_with_api_key("opaque-s3cret", ["platform-admins"])
    # Opaque token in the standard Authorization: Bearer header.
    req = SimpleNamespace(headers={"Authorization": "Bearer opaque-s3cret"})
    p = require_auth(req, settings, validator=None)  # validator unused for opaque key
    assert p.username == "ci-bot" and p.is_admin is True

    bad = SimpleNamespace(headers={"Authorization": "Bearer nope"})
    with pytest.raises(UnauthenticatedError):
        require_auth(bad, settings, validator=None)


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
            SiteConfig(name="site-a", api_server="https://a"),
            SiteConfig(name="site-b", api_server="https://b"),
        ]
    )


def test_global_cert_and_ca_paths():
    s = Settings(client_cert_dir="/etc/serverless/client")
    assert s.client_cert_path == "/etc/serverless/client/tls.crt"
    assert s.client_key_path == "/etc/serverless/client/tls.key"
    assert s.ca_bundle.path == "/etc/serverless/trusted-ca/ca-bundle.crt"


def test_resolve_targets_default_all():
    d = Deployer(_settings_with_sites())
    assert [z.name for z in d.resolve_targets(None)] == ["site-a", "site-b"]


def test_resolve_targets_unknown_site():
    d = Deployer(_settings_with_sites())
    with pytest.raises(ValidationError):
        d.resolve_targets(["site-c"])


async def test_fanout_captures_per_site_errors():
    d = Deployer(_settings_with_sites())

    def fn(client, site):
        if site.name == "site-b":
            raise RuntimeError("kaboom")
        return SiteStatus(site=site.name, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_site = {s.site: s for s in statuses}
    assert by_site["site-a"].status == "Ready"
    assert by_site["site-b"].error == "kaboom"


async def test_fanout_times_out_unreachable_site():
    import time

    d = Deployer(_settings_with_sites())
    d._op_timeout = 0.05  # tighten for the test

    def fn(client, site):
        if site.name == "site-b":
            time.sleep(0.5)  # simulate an unreachable/slow cluster
        return SiteStatus(site=site.name, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_site = {s.site: s for s in statuses}
    # the healthy site still returns; the slow one is reported, not blocking
    assert by_site["site-a"].status == "Ready"
    assert by_site["site-b"].status == "Timeout"
    assert by_site["site-b"].error is not None


class _FakeCluster:
    def __init__(self, name, existing=None):
        self.name = name
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
    d.cluster = lambda site: clusters[site.name]  # inject fakes
    return WorkloadService(settings, d, FuncBuilder(settings))


async def test_host_available_when_unused():
    svc = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    # no DomainMapping exists -> no raise
    await svc._assert_host_available(
        "app-team.serverless.example.com", "app-team", svc._deployer.resolve_targets(None)
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
        await svc._assert_host_available(host, "app-team", svc._deployer.resolve_targets(None))


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
    await svc._assert_host_available(host, "app-team", svc._deployer.resolve_targets(None))


async def test_workload_absent_ok():
    svc = _workload_service(
        {"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")}
    )
    await svc._assert_workload_absent(
        "app", "app-team", svc._deployer.resolve_targets(None)
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
        await svc._assert_workload_absent(
            "app", "app-team", svc._deployer.resolve_targets(None)
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
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("caas")}),
            "site-b": _FakeCluster("site-b", existing={"app-team": _ksvc("caas")}),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    existing = await svc._load_existing("app", "caas", user)
    assert existing["image"] == "reg/x:1"


async def test_load_existing_offering_mismatch_404():
    from app.auth.claims import Principal
    from app.core.errors import NotFoundError

    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("caas")}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await svc._load_existing("app", "faas", user)  # it's a caas


async def test_accept_container_returns_pending_and_schedules():
    from fastapi import BackgroundTasks

    from app.auth.claims import Principal
    from app.models.container import ContainerCreate

    svc = _workload_service(
        {"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")}
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    spec = ContainerCreate(
        name="app", image="reg/x:1", registryUsername="u", registryToken="t"
    )
    body = await svc.accept_container(spec, user, bg)
    assert body.overallStatus == "Pending"
    assert body.statusUrl == "/api/v1/containers/app/status"
    assert len(bg.tasks) == 1  # deploy scheduled in the background
