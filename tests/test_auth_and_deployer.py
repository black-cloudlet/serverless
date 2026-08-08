import pytest

from api.core.config import Settings, SiteConfig, SSOSettings
from api.models.common import SiteStatus
from api.services.offering import CONTAINER, FUNCTION
from api.services.sites.deployer import Deployer, aggregate, status_code_for
from common.errors import SiteTotalFailure, ValidationError
from tests.conftest import runtime_registry


def _auth_with_admin_key(monkeypatch, raw_key, admin_groups=("platform-admins",)):
    """Point api.auth.deps at settings carrying ``raw_key``, as production does.

    Goes through ``get_auth`` rather than building an ``SSOAuth`` here, so what
    is under test is this API's wiring - which settings the shared component is
    built from - and not a second copy of it that could quietly drift.
    """
    from api.auth.deps import get_auth

    settings = Settings(
        auth_enabled=True,
        admin_api_key=raw_key,
        sso=SSOSettings(admin_groups=list(admin_groups)),
    )
    monkeypatch.setattr("api.auth.deps.get_settings", lambda: settings)
    get_auth.cache_clear()


def test_require_auth_via_bearer_admin_key(monkeypatch):
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "opaque-s3cret")
    # Opaque admin key in the standard Authorization: Bearer header.
    req = SimpleNamespace(headers={"Authorization": "Bearer opaque-s3cret"})
    p = require_auth(req)
    assert p.username == "admin" and p.is_admin is True
    # The configured admin groups, mapped the same way a token's claim is: the
    # normalized name as the key, the spelling config used underneath.
    assert p.groups == {"platform-admins": "platform-admins"}
    assert p.can_access_group("platform-admins") is True

    # An unrecognised, non-JWT token must be rejected, not silently allowed.
    bad = SimpleNamespace(headers={"Authorization": "Bearer nope"})
    with pytest.raises(UnauthenticatedError):
        require_auth(bad)


def test_admin_key_header_carries_raw_token_not_a_hash(monkeypatch):
    """The header must carry the RAW token, matched directly against the configured
    key. A sha256 hash of the key is not the key, so it must not authenticate."""
    import hashlib
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "opaque-s3cret")
    hashed = hashlib.sha256(b"opaque-s3cret").hexdigest()
    req = SimpleNamespace(headers={"Authorization": f"Bearer {hashed}"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req)


def test_empty_admin_key_disables_key_auth(monkeypatch):
    """Setting the admin key empty disables API-key auth entirely; no opaque token
    (including the empty string) is accepted."""
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "")
    req = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req)


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
    from api.services.sites.deployer import overall_status

    assert overall_status(["Ready", "Ready"]) == "Ready"
    assert overall_status(["Deploying", "Deploying"]) == "Deploying"
    # a normal rollout where one site is ahead is still in-progress, not Degraded
    assert overall_status(["Ready", "Deploying"]) == "Deploying"
    # any failed (or unreachable, mapped to Failed) site -> Degraded
    assert overall_status(["Ready", "Failed"]) == "Degraded"
    assert overall_status(["Deploying", "Failed"]) == "Degraded"
    assert overall_status([]) == "Degraded"


def test_stats_code_for_deploying_is_non_terminal():
    # Deploying is an accepted, still-rolling-out state, not a partial failure
    assert status_code_for("Deploying", created=True) == 202
    assert status_code_for("Deploying", created=False) == 202
    assert status_code_for("Ready", created=False) == 200


def test_ksvc_status_distinguishes_failed_from_deploying():
    from api.services.state.ksvc_state import ksvc_status

    def _obj(ready_status):
        conditions = [{"type": "Ready", "status": ready_status}] if ready_status else []
        return {"status": {"conditions": conditions, "latestCreatedRevisionName": "rev-1"}}

    assert ksvc_status(_obj("True")) == ("Ready", "rev-1")
    assert ksvc_status(_obj("False")) == ("Failed", "rev-1")  # terminal failure
    assert ksvc_status(_obj("Unknown")) == ("Deploying", "rev-1")  # still progressing
    assert ksvc_status(_obj(None)) == ("Deploying", "rev-1")  # no condition yet
    assert ksvc_status({}) == ("Deploying", None)  # brand-new, no status block


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
        from common.errors import NotFoundError as _NF

        if name in self._existing:
            return self._existing[name]
        raise _NF(f"{name} not found")


class _NullBuilder:
    """Builder that declares nothing; for the non-function paths."""

    pull_secret = "reg-creds"

    def image_ref(self, req):
        return "reg/built:1"

    def plan(self, req, labels):
        from common.build import BuildPlan

        return BuildPlan(tag=self.image_ref(req), replicated=[], local=[])

    def status(self, cluster, name, group):
        return None

    def statuses(self, cluster, group):
        return {}


def _workload_service(clusters, builder=None, local_site=None):
    from api.services.workloads import WorkloadService

    settings = _settings_with_sites()
    d = Deployer(settings)
    d._clusters = clusters  # inject fakes (name -> _FakeCluster)
    d._local_site = local_site
    return WorkloadService(settings, d, builder or _NullBuilder())


def test_host_for_resolution_and_validation():
    from common.errors import ValidationError

    svc = _workload_service({})  # host_for doesn't touch clusters

    # no hostname -> default {name}-{group}.{route_domain}
    assert svc.host_for("app", None, "team") == "app-team.serverless.example.com"
    # single label (last octet) -> base domain appended
    assert svc.host_for("app", "shop", "team") == "shop.serverless.example.com"
    # one label under the base domain -> kept as-is
    assert (
        svc.host_for("app", "shop.serverless.example.com", "team") == "shop.serverless.example.com"
    )
    # FQDN outside the base domain -> rejected (surfaced as 400)
    with pytest.raises(ValidationError):
        svc.host_for("app", "shop.evil.com", "team")
    # more than one level under the base domain -> rejected
    with pytest.raises(ValidationError):
        svc.host_for("app", "a.b.serverless.example.com", "team")


def test_a_name_and_group_each_legal_alone_are_rejected_when_too_long_together():
    """{name}-{group} is the KSVC name and the first label of the host: max 63.

    Each half passes its own <=63 check, so without a combined one the request is
    accepted (202) and the API server rejects the KSVC later, in the background
    deploy - after the config Secrets for it have already been written.
    """
    from common.errors import ValidationError

    svc = _workload_service({})
    name, group = "n" * 40, "g" * 40

    with pytest.raises(ValidationError, match="too long together"):
        svc.validate_spec(name, group, "alice", [], [])

    # 63 exactly is the boundary and must still pass.
    svc.validate_spec("n" * 31, "g" * 31, "alice", [], [])


async def test_host_available_when_unused():
    svc = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    # no DomainMapping exists -> no raise
    await svc.assert_host_available(
        "app-team.serverless.example.com", "app", "team", svc.deployer.resolve_targets(None)
    )


async def test_host_taken_by_other_workload_conflicts():
    from api.models.common import LABEL_WORKLOAD
    from common.errors import ConflictError

    host = "shared.example.com"
    dm = {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "other-team"}}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={host: dm}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    with pytest.raises(ConflictError):
        await svc.assert_host_available(host, "app", "team", svc.deployer.resolve_targets(None))


async def test_host_owned_by_same_workload_ok():
    from api.models.common import LABEL_WORKLOAD

    host = "app-team.serverless.example.com"
    dm = {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "app-team"}}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={host: dm}),
            "site-b": _FakeCluster("site-b", existing={host: dm}),
        }
    )
    # same owner -> update, no conflict
    await svc.assert_host_available(host, "app", "team", svc.deployer.resolve_targets(None))


async def test_workload_absent_ok():
    svc = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    await svc.assert_workload_absent("app", "team", svc.deployer.resolve_targets(None))


async def test_workload_already_exists_conflicts():
    from common.errors import ConflictError

    ksvc = {"metadata": {"name": "app-team"}}
    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": ksvc}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    with pytest.raises(ConflictError):
        await svc.assert_workload_absent("app", "team", svc.deployer.resolve_targets(None))


def _ksvc(offering, image="reg/x:1", group="team"):
    from api.models.common import LABEL_GROUP, LABEL_OFFERING

    return {
        "metadata": {"name": "app-team", "labels": {LABEL_GROUP: group, LABEL_OFFERING: offering}},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
    }


async def test_load_existing_returns_image():
    from cloudlet_apis.auth import Principal

    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("container")}),
            "site-b": _FakeCluster("site-b", existing={"app-team": _ksvc("container")}),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    existing = await svc.load_existing("app", CONTAINER, user, "team")
    assert existing["image"] == "reg/x:1"


async def test_load_existing_offering_mismatch_404():
    from cloudlet_apis.auth import Principal

    from common.errors import NotFoundError

    svc = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ksvc("container")}),
            "site-b": _FakeCluster("site-b"),
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await svc.load_existing("app", FUNCTION, user, "team")  # it's a container


async def test_accept_container_returns_pending_and_schedules():
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService

    engine = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    svc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )
    body = await svc.accept("team", spec, user, bg)
    assert body.overallStatus == "Pending"
    assert body.statusUrl == "/api/v1/groups/team/containers/app"
    assert len(bg.tasks) == 1  # deploy scheduled in the background


async def test_get_reports_size_and_replicas_but_carries_no_usage():
    """The full GET keeps `replicas` and drops `usage`.

    Not an arbitrary split: `replicas` rides along on the Revision read the
    per-site failure detail needs anyway, so it is free, while usage is a
    PodMetrics call of its own. Reading PodMetrics here is an assertion failure -
    that is what keeps the cost off a response nobody polls for live numbers.
    """
    from cloudlet_apis.auth import Principal

    from api.models.common import ANNOTATION_HOST, ANNOTATION_SIZE, LABEL_GROUP, LABEL_OFFERING
    from common.cluster import ResourceKind

    class _NoMetricsCluster:
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
                return {"status": {"actualReplicas": 3}}
            raise AssertionError(f"the full GET must not read {kind}")

    engine = _workload_service({"site-a": _NoMetricsCluster("site-a")})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.size == "medium"
    site = body.sites[0]
    assert site.replicas == 3  # from Revision.status.actualReplicas
    assert not hasattr(site, "usage")  # live usage is a /status field now


class _StatsCluster:
    """Serves the three reads the stats view is allowed to make, and only those.

    Any other kind is an assertion failure - what keeps a spec read (a ConfigMap,
    or the backing Secret) from creeping into a path a client polls every few
    seconds.
    """

    def __init__(self, name, pods=None, replicas=2, offering="container", group="team"):
        self.site = name
        self.name = name
        self._pods = pods
        self._replicas = replicas
        self._offering = offering
        self._group = group

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from api.models.common import LABEL_GROUP, LABEL_OFFERING
        from common.cluster import ResourceKind

        if kind == ResourceKind.KNATIVE_SERVICE:
            return {
                "metadata": {
                    "name": name,
                    "labels": {LABEL_GROUP: self._group, LABEL_OFFERING: self._offering},
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "latestReadyRevisionName": "app-team-00002",
                },
            }
        if kind == ResourceKind.KNATIVE_REVISION:
            return {"status": {"actualReplicas": self._replicas}}
        if kind == ResourceKind.POD_METRICS:
            assert label_selector == "serving.knative.dev/service=app-team"
            return list(self._pods or [])
        raise AssertionError(f"the stats view must not read {kind}")


def _metrics_pod(cpu, memory):
    return {
        "containers": [
            {"name": "user-container", "usage": {"cpu": cpu, "memory": memory}},
            {"name": "queue-proxy", "usage": {"cpu": "999m", "memory": "999Mi"}},
        ]
    }


async def test_stats_returns_live_state_and_reads_nothing_else():
    from cloudlet_apis.auth import Principal

    engine = _workload_service(
        {"site-a": _StatsCluster("site-a", pods=[_metrics_pod("60m", "90Mi")] * 2)}
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(CONTAINER, "app", user, "team")

    assert body.overallStatus == "Ready"
    assert body.replicas == 2
    # summed over the user containers, ignoring the queue-proxy sidecar
    assert (body.usage.cpu, body.usage.memory) == ("120m", "180Mi")
    site = body.sites[0]
    assert (site.site, site.status, site.replicas) == ("site-a", "Ready", 2)
    assert (site.usage.cpu, site.usage.memory) == ("120m", "180Mi")


async def test_stats_totals_across_sites_from_the_raw_figures():
    """The workload total is summed before rounding, so it is not 0m here."""
    from cloudlet_apis.auth import Principal

    def site(name):
        return _StatsCluster(name, pods=[_metrics_pod("500u", "1536Ki")] * 2)

    engine = _workload_service({"site-a": site("site-a"), "site-b": site("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(CONTAINER, "app", user, "team")

    assert body.sites[0].usage.cpu == "1m"  # each site holds 2 x 0.5m
    assert body.usage.cpu == "2m"
    assert body.usage.memory == "6Mi"
    assert body.replicas == 4


async def test_stats_total_is_null_when_a_site_could_not_be_measured():
    """A total that quietly drops a site is worse than no total."""
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

    class _NoMetrics(_StatsCluster):
        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.POD_METRICS:
                raise RuntimeError("metrics API unavailable")
            return super().get(kind, name, label_selector, namespace)

    good = _StatsCluster("site-a", pods=[_metrics_pod("60m", "90Mi")])
    engine = _workload_service({"site-a": good, "site-b": _NoMetrics("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(CONTAINER, "app", user, "team")

    assert body.usage is None  # not "60m", which would understate the workload
    assert body.sites[0].usage.cpu == "60m"  # what site-a reported still stands
    assert body.sites[1].usage is None
    assert body.replicas == 4  # replicas came off the Revision, which answered


async def test_stats_survives_a_site_that_is_entirely_down():
    from cloudlet_apis.auth import Principal

    class _Down:
        site = name = "site-b"

        def get(self, *a, **k):
            raise RuntimeError("site down")

    up = _StatsCluster("site-a", pods=[_metrics_pod("60m", "90Mi")])
    engine = _workload_service({"site-a": up, "site-b": _Down()})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(CONTAINER, "app", user, "team")

    assert body.overallStatus == "Degraded"
    by_site = {s.site: s for s in body.sites}
    assert by_site["site-a"].usage.cpu == "60m"  # the healthy site still reports
    assert by_site["site-b"].status == "Failed"
    assert by_site["site-b"].usage is None
    assert body.usage is None and body.replicas is None


async def test_stats_scaled_to_zero_is_not_an_error():
    from cloudlet_apis.auth import Principal

    engine = _workload_service({"site-a": _StatsCluster("site-a", pods=[], replicas=0)})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(CONTAINER, "app", user, "team")

    assert body.replicas == 0
    assert body.usage is None  # nothing running, so nothing measured
    assert body.sites[0].usage is None


async def test_stats_folds_a_running_build_into_the_rollup_and_the_sites():
    """Build-first, on both surfaces - matching the full GET (#38).

    A function whose image is not built yet has a KSVC that cannot pull it. Left
    unfolded the header would read Building over a site row saying Failed.
    """
    from cloudlet_apis.auth import Principal

    from common.build import BuildStatus
    from common.cluster import ResourceKind

    class _C:
        site = name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            from api.models.common import LABEL_GROUP, LABEL_OFFERING

            if kind == ResourceKind.KNATIVE_SERVICE:
                return {
                    "metadata": {
                        "name": name,
                        "labels": {LABEL_GROUP: "team", LABEL_OFFERING: "function"},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                }
            raise RuntimeError("revision/metrics are best-effort")

    class _BuildingBuilder(_NullBuilder):
        def status(self, cluster, name, group):
            return BuildStatus(state="Building")

    engine = _workload_service({"site-a": _C()}, builder=_BuildingBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.stats(FUNCTION, "fn", user, "team")

    assert body.overallStatus == "Building"  # not Degraded, and not Deploying
    assert body.sites[0].status == "Building"  # and the row agrees with the header


async def test_stats_hides_another_groups_workload_as_404():
    from cloudlet_apis.auth import Principal

    from common.errors import NotFoundError

    engine = _workload_service({"site-a": _StatsCluster("site-a", pods=[], group="other")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await engine.stats(CONTAINER, "app", user, "team")


async def test_stats_of_an_unconfirmable_workload_fails_closed():
    """A site that cannot answer means "absent" is not established -> 503, not 404."""
    from cloudlet_apis.auth import Principal

    from common.errors import ServiceUnavailableError

    class _Down:
        site = "site-b"
        name = "site-b"

        def get(self, *a, **k):
            raise RuntimeError("site down")

    engine = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _Down()})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(ServiceUnavailableError):
        await engine.stats(CONTAINER, "app", user, "team")


def _list_ksvc(oname, size, host, ready=True):
    from api.models.common import ANNOTATION_HOST, ANNOTATION_SIZE, LABEL_GROUP, LABEL_OFFERING

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
        from common.cluster import ResourceKind

        assert kind == ResourceKind.KNATIVE_SERVICE
        return list(self._items)


async def test_get_returns_redacted_spec():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests.files import VolumeSpec
    from api.services.manifests.ksvc import ContainerEnv, build_ksvc
    from common.cluster import ResourceKind

    ksvc = build_ksvc(
        name="app-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="app-team.ex.com",
        env=[
            ContainerEnv(name="LOG", value="debug"),
            ContainerEnv(name="API_KEY", secret_ref=("app-team-env", "API_KEY")),
        ],
        volumes=[
            VolumeSpec(
                "files-config", "configmap", "app-team-files", "/etc/app.conf", "etc-app.conf", True
            ),
            VolumeSpec(
                "files-secret", "secret", "app-team-files", "/etc/secret", "etc-secret", True
            ),
        ],
        scaling=Scaling(minScale=1, maxScale=4, metric="cpu", target=80),
        size="medium",
        pull_secret="app-team-pull",
        ca_config_map="trusted-ca",
        ca_mount_path="/etc/pki/tls/certs/ca.crt",
    )

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            from api.services.manifests.secrets import build_pull_secret

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
    body = await engine.get(CONTAINER, "app", user, "team")

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


def _bare_ksvc(name="app-team", offering="container"):
    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc

    return build_ksvc(
        name=name,
        group="team",
        owner="alice",
        image="reg/app:1",
        offering=offering,
        host=f"{name}.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )


async def test_get_function_returns_build_inputs_and_build_state():
    """GET on a function reads the whole function-only branch of the response.

    The container variant is covered several times over; this drives the branch
    that only a function reaches - the build-input annotations projected through
    WorkloadSpec, and the kpack build status folded into overallStatus.
    """
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.build import BuildStatus
    from common.cluster import ResourceKind

    ksvc = build_ksvc(
        name="fn-team",
        group="team",
        owner="alice",
        image="reg/team/fn:main",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        version="3.13",
        git_url="https://git.example.com/team/monorepo.git",
        branch="main",
        path="services/api",
    )
    ksvc["status"] = {
        "conditions": [{"type": "Ready", "status": "True"}],
        "latestReadyRevisionName": "fn-team-00001",
    }

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            raise RuntimeError("revision/metrics/configmaps are best-effort here")

    class _ReadyBuilder(_NullBuilder):
        def status(self, cluster, name, group):
            return BuildStatus(state="Ready", image="reg/team/fn@sha256:abc")

    engine = _workload_service({"site-a": _C()}, builder=_ReadyBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(FUNCTION, "fn", user, "team")

    assert body.runtime == "python"
    assert body.gitRepo == "https://git.example.com/team/monorepo.git"
    assert body.branch == "main"
    # The sub-directory a monorepo function builds from, round-tripped through
    # the annotation and WorkloadSpec rather than dropped on the floor.
    assert body.path == "services/api"
    # the version the caller pinned, round-tripped through the annotation
    assert body.version == "3.13"
    assert body.build.state == "Ready"
    assert body.overallStatus == "Ready"
    # the built image stays internal - a function's client deals in source
    assert not hasattr(body, "image")


async def test_get_function_building_image_reports_building():
    """A function whose image is still building is Building, not Degraded."""
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.build import BuildStatus
    from common.cluster import ResourceKind

    ksvc = build_ksvc(
        name="fn-team",
        group="team",
        owner="alice",
        image="reg/team/fn:main",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        git_url="https://git.example.com/team/fn.git",
        branch="main",
    )
    # KSVC can't pull an image that does not exist yet -> Ready=False
    ksvc["status"] = {
        "conditions": [
            {
                "type": "Ready",
                "status": "False",
                "message": 'Unable to fetch image "reg/team/fn:main": not found',
            }
        ]
    }

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            raise RuntimeError("best-effort reads")

    class _BuildingBuilder(_NullBuilder):
        def status(self, cluster, name, group):
            return BuildStatus(state="Building")

    engine = _workload_service({"site-a": _C()}, builder=_BuildingBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(FUNCTION, "fn", user, "team")

    assert body.build.state == "Building"
    assert body.overallStatus == "Building"
    # The per-site row agrees with the headline instead of contradicting it: the
    # KSVC's pull failure IS the running build, not a second, independent one.
    assert body.sites[0].status == "Building"
    assert body.sites[0].error is None
    assert body.path is None  # no sub-directory -> built from the repository root
    assert body.version is None  # took the platform default; /info says what that is


async def test_get_overall_status_reflects_rollout_state():
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

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
        return (await engine.get(CONTAINER, "app", user, "team")).overallStatus

    # no Ready condition yet -> Deploying (regression: this used to be Degraded)
    assert await _overall() == "Deploying"
    # Ready=True -> Ready
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    assert await _overall() == "Ready"
    # Ready=False -> terminal failure surfaces as Degraded
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}
    assert await _overall() == "Degraded"


async def test_get_failed_site_surfaces_ready_condition_message():
    """A reachable site whose KSVC failed to roll out carries the Ready-condition
    reason in the per-site `error` (not a bare status=Failed, error=null)."""
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

    ksvc = _bare_ksvc()
    ksvc["status"] = {
        "conditions": [
            {
                "type": "Ready",
                "status": "False",
                "reason": "RevisionFailed",
                "message": 'Revision "app-team-00001" failed: image pull backoff',
            }
        ]
    }

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            raise RuntimeError("replicas/usage/spec extras are best-effort here")

    engine = _workload_service({"site-a": _C()})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")

    site = body.sites[0]
    assert site.status == "Failed"
    assert site.error == 'Revision "app-team-00001" failed: image pull backoff'

    # Falls back to the reason code when the condition carries no message.
    ksvc["status"]["conditions"][0].pop("message")
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.sites[0].error == "RevisionFailed"


async def test_get_failed_site_prefers_revision_specific_reason():
    """The per-site error is the Revision's specific failing sub-condition (the
    real cause), not the KSVC's generic aggregate message."""
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

    ksvc = _bare_ksvc()
    ksvc["status"] = {
        "latestCreatedRevisionName": "app-team-00001",
        "conditions": [
            # Generic aggregate at the Service level.
            {
                "type": "Ready",
                "status": "False",
                "reason": "RevisionFailed",
                "message": "Revision app-team-00001 is not ready.",
            },
        ],
    }
    revision = {
        "metadata": {"name": "app-team-00001"},
        "status": {
            "actualReplicas": 0,
            "conditions": [
                {"type": "Ready", "status": "False", "message": "Revision not ready."},
                # The specific cause we want surfaced.
                {
                    "type": "ContainerHealthy",
                    "status": "False",
                    "reason": "ImagePullBackOff",
                    "message": 'Unable to fetch image "reg/app:1": not found',
                },
            ],
        },
    }

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            if kind == ResourceKind.KNATIVE_REVISION and name == "app-team-00001":
                return revision
            raise RuntimeError("usage/spec extras are best-effort here")

    engine = _workload_service({"site-a": _C()})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")

    site = body.sites[0]
    assert site.status == "Failed"
    assert site.error == 'Unable to fetch image "reg/app:1": not found'
    assert site.replicas == 0  # same Revision read still feeds the replica count


async def test_get_ready_site_has_no_error():
    """A healthy (Ready) site leaves `error` null - it's only set on failure."""
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

    ksvc = _bare_ksvc()
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}

    class _C:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            raise RuntimeError("best-effort extras")

    engine = _workload_service({"site-a": _C()})
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.sites[0].status == "Ready" and body.sites[0].error is None


async def test_list_overall_status_per_workload():
    from cloudlet_apis.auth import Principal

    deploying = _bare_ksvc("app-team")  # no conditions -> Deploying
    failed = _bare_ksvc("bad-team")
    failed["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}

    engine = _workload_service(
        {"site-a": _ListCluster("site-a", [deploying, failed])}, local_site="site-a"
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    summaries = {s.name: s.overallStatus for s in await engine.list(CONTAINER, user, "team")}

    assert summaries["app"] == "Deploying"  # not a false Degraded
    assert summaries["bad"] == "Degraded"


async def test_list_folds_the_build_state_in_like_get_does():
    """A listing is build-first too: a first build reads Building, not Degraded.

    Regression: the console's function cards showed a red `Degraded` for the whole
    of a normal first build, because the list read only the KSVC - which cannot
    pull an image kpack has not pushed yet. The single GET already folded the
    build in; the list did not, so the two disagreed about the same function.
    """
    from cloudlet_apis.auth import Principal

    from common.build import BuildStatus

    building = _bare_ksvc("fn-team", offering="function")
    building["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}
    broken = _bare_ksvc("bad-team", offering="function")
    broken["status"] = {"conditions": [{"type": "Ready", "status": "False"}]}
    serving = _bare_ksvc("old-team", offering="function")
    serving["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}

    reads = []

    class _Builder(_NullBuilder):
        def statuses(self, cluster, group):
            # One read for the whole listing, not one per function.
            reads.append((cluster.site, group))
            return {
                "fn-team": BuildStatus(state="Building"),
                "bad-team": BuildStatus(state="Failed", message="compile error"),
            }

    engine = _workload_service(
        {"site-a": _ListCluster("site-a", [building, broken, serving])},
        builder=_Builder(),
        local_site="site-a",
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    summaries = {s.name: s.overallStatus for s in await engine.list(FUNCTION, user, "team")}

    assert summaries["fn"] == "Building"
    # a failed build is the honest cause of the same symptom -> still Degraded
    assert summaries["bad"] == "Degraded"
    # no build in flight -> the KSVC has the last word, as after a switchover
    assert summaries["old"] == "Ready"
    assert reads == [("site-a", "team")]  # local site only, once


async def test_list_of_containers_reads_no_build():
    """An offering with no build never pays for the build read (`has_build`)."""
    from cloudlet_apis.auth import Principal

    class _Builder(_NullBuilder):
        def statuses(self, cluster, group):
            raise AssertionError("a container listing must not read the build backend")

    engine = _workload_service(
        {"site-a": _ListCluster("site-a", [_bare_ksvc("app-team")])},
        builder=_Builder(),
        local_site="site-a",
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    summaries = await engine.list(CONTAINER, user, "team")

    assert [s.name for s in summaries] == ["app"]


class _ApplyCluster:
    """Records applied manifests; serves a preset existing KSVC (and Secrets)."""

    def __init__(self, name, existing, secrets=None, images=None):
        self.site = name
        self.name = name
        self._existing = existing  # oname -> ksvc dict
        self._secrets = secrets or {}  # secret name -> secret dict (preset)
        self._images = images or {}  # kpack Image name -> object (preset)
        self.applied = []
        self.deleted = []  # [(ResourceKind, name)] from prune / re-tag

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from common.cluster import ResourceKind
        from common.errors import NotFoundError as _NF

        if kind == ResourceKind.KNATIVE_SERVICE and name in self._existing:
            return self._existing[name]
        if kind == ResourceKind.SECRET and name in self._secrets:
            return self._secrets[name]
        if kind == ResourceKind.KPACK_IMAGE and name in self._images:
            return self._images[name]
        raise _NF("not found")  # domain mapping -> Available; missing ksvc/secret/image

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
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar, FileMount, Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc
    from common.cluster import ResourceKind

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # New spec keeps only a PLAIN env var and a NON-secret file -> no env Secret,
    # a files ConfigMap, but no files Secret.
    await csvc.update(
        "team",
        "api",
        ContainerUpdate(
            image="reg/x:1",
            port=8080,
            env=[EnvVar(name="LOG", value="debug")],  # plain -> no env Secret
            files=[FileMount(mountPath="/etc/app.conf", content="x")],  # -> files ConfigMap
        ),
        user,
    )

    deleted = set(cluster.deleted)
    assert (ResourceKind.SECRET, "api-team-env") in deleted  # no secret env -> pruned
    assert (ResourceKind.SECRET, "api-team-files") in deleted  # no secret files -> pruned
    # the files ConfigMap is still referenced -> applied, never pruned
    assert (ResourceKind.CONFIG_MAP, "api-team-files") not in deleted
    assert any(
        m["kind"] == "ConfigMap" and m["metadata"]["name"] == "api-team-files"
        for m in cluster.applied
    )


async def test_create_does_not_prune():
    """On create there is nothing to prune; no deletes are issued."""
    from cloudlet_apis.auth import Principal

    from api.services.container import ContainerService

    cluster = _ApplyCluster("site-a", {})  # nothing exists yet
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    from api.models.container import ContainerCreate

    await csvc.create("team", ContainerCreate(name="api", image="reg/x:1", port=8080), user)
    assert cluster.deleted == []


async def test_container_update_rotates_pull_secret():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg.acme.com/api:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",  # public image: no pull secret
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    from api.models.container import ContainerUpdate

    await csvc.update(
        "team",
        "api",
        ContainerUpdate(
            image="reg.acme.com/team/app:2", port=8080, registryUsername="bob", registryToken="t"
        ),
        user,
    )

    secrets = [
        m
        for m in _applied_kind(cluster, "Secret")
        if m.get("type") == "kubernetes.io/dockerconfigjson"
    ]
    assert secrets and secrets[0]["metadata"]["name"] == "api-team-pull"
    # the pull secret is keyed to the client image's registry, not our platform one
    import base64 as _b64
    import json as _json

    cfg = _json.loads(_b64.b64decode(secrets[0]["data"][".dockerconfigjson"]))
    assert set(cfg["auths"]) == {"reg.acme.com"}
    ksvc = _applied_kind(cluster, "Service")[0]
    assert ksvc["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "api-team-pull"}]


async def test_function_update_rebuilds_without_touching_the_running_image():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.state.ksvc_state import extract_image

    class _StubBuilder(_NullBuilder):
        def __init__(self):
            self.calls = 0

        def image_ref(self, req):
            return "reg/built:rel"

        def plan(self, req, labels):
            from common.build import BuildPlan

            self.calls += 1
            self.req = req
            return BuildPlan(tag=self.image_ref(req), replicated=[], local=[])

    existing = build_ksvc(
        name="fn-team",
        group="team",
        owner="alice",
        image="reg/fn:old",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        git_url="https://git/old.git",
        branch="main",
    )
    cluster = _ApplyCluster("site-a", {"fn-team": existing})
    builder = _StubBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    fsvc = FunctionService(engine, runtime_registry())
    user = Principal(subject="u", username="alice", groups=["team"])

    # rebuild from a new branch; gitRepo/runtime re-sent unchanged (full replace)
    await fsvc.update(
        "team",
        "fn",
        FunctionUpdate(
            gitRepo="https://git/old.git", runtime="python", branch="release", gitToken="tok"
        ),
        user,
    )
    assert builder.calls == 1
    assert builder.req.branch == "release"
    assert builder.req.git_url == "https://git/old.git"
    assert builder.req.runtime == "python"
    ksvc = _applied_kind(cluster, "Service")[0]
    # The build is re-declared, the running image is not touched: after the
    # create only the controller writes it (docs/BUILDING.md - Digest propagation).
    assert extract_image(ksvc) == "reg/fn:old"


async def test_function_update_without_token_keeps_image():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.state.ksvc_state import extract_image

    class _StubBuilder(_NullBuilder):
        def __init__(self):
            self.calls = 0

        def plan(self, req, labels):
            self.calls += 1
            raise AssertionError("no token stored -> nothing to declare a build with")

    existing = build_ksvc(
        name="fn-team",
        group="team",
        owner="alice",
        image="reg/fn:old",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        git_url="https://git/old.git",
        branch="main",
    )
    cluster = _ApplyCluster("site-a", {"fn-team": existing})
    builder = _StubBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    fsvc = FunctionService(engine, runtime_registry())
    user = Principal(subject="u", username="alice", groups=["team"])

    # config-only edit re-sends the same build inputs -> no rebuild
    await fsvc.update(
        "team",
        "fn",
        FunctionUpdate(
            gitRepo="https://git/old.git",
            runtime="python",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        user,
    )
    assert builder.calls == 0
    ksvc = _applied_kind(cluster, "Service")[0]
    assert extract_image(ksvc) == "reg/fn:old"  # existing image preserved


async def test_function_create_persists_git_secret():
    import base64

    from cloudlet_apis.auth import Principal

    from api.models.function import FunctionCreate
    from api.services.function import FunctionService
    from api.services.manifests.secrets import GIT_TOKEN_KEY, build_git_secret

    class _StubBuilder(_NullBuilder):
        def plan(self, req, labels):
            from common.build import BuildPlan

            # the git Secret is now part of what the builder declares
            return BuildPlan(
                tag=self.image_ref(req),
                replicated=[build_git_secret("fn-team-git", labels, req.git_token, req.git_url)],
                local=[],
            )

    cluster = _ApplyCluster("site-a", {})  # nothing exists yet
    engine = _workload_service({"site-a": cluster}, builder=_StubBuilder())
    fsvc = FunctionService(engine, runtime_registry())
    user = Principal(subject="u", username="alice", groups=["team"])

    await fsvc.create(
        "team",
        FunctionCreate(
            name="fn", gitRepo="https://git/x.git", gitToken="ghp_tok", runtime="python"
        ),
        user,
    )
    git = [s for s in _applied_kind(cluster, "Secret") if s["metadata"]["name"] == "fn-team-git"]
    assert git, "expected a persisted git secret"
    assert base64.b64decode(git[0]["data"][GIT_TOKEN_KEY]).decode() == "ghp_tok"


async def test_function_update_reuses_stored_git_token():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.manifests.secrets import build_git_secret, git_secret_name

    class _StubBuilder(_NullBuilder):
        def __init__(self):
            self.calls = 0

        def plan(self, req, labels):
            from common.build import BuildPlan

            self.calls += 1
            self.req = req
            return BuildPlan(tag="reg/built:rel", replicated=[], local=[])

    existing = build_ksvc(
        name="fn-team",
        group="team",
        owner="alice",
        image="reg/fn:old",
        offering="function",
        host="fn-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        git_url="https://git/old.git",
        branch="main",
    )
    stored = build_git_secret(git_secret_name("fn-team"), {}, "ghp_stored")
    cluster = _ApplyCluster("site-a", {"fn-team": existing}, secrets={"fn-team-git": stored})
    builder = _StubBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    fsvc = FunctionService(engine, runtime_registry())
    user = Principal(subject="u", username="alice", groups=["team"])

    # change the branch WITHOUT re-supplying a token -> rebuild uses the stored one
    await fsvc.update(
        "team",
        "fn",
        FunctionUpdate(gitRepo="https://git/old.git", runtime="python", branch="release"),
        user,
    )
    assert builder.calls == 1
    assert builder.req.branch == "release"
    assert builder.req.git_token == "ghp_stored"  # reused, not re-supplied


async def test_container_update_keeps_secret_env_value_when_omitted():
    import base64

    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar, Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests import resources as res
    from api.services.manifests.ksvc import ContainerEnv, build_ksvc

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[ContainerEnv(name="API_KEY", secret_ref=("api-team-env", "API_KEY"))],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    env_secret = res.build_secret("api-team-env", {}, {"API_KEY": "stored-secret"})
    cluster = _ApplyCluster("site-a", {"api-team": existing}, secrets={"api-team-env": env_secret})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # echo the redacted read back: secret env var with no value -> keep stored value
    await csvc.update(
        "team",
        "api",
        ContainerUpdate(image="reg/x:1", port=8080, env=[EnvVar(name="API_KEY", secret=True)]),
        user,
    )
    applied_env = [
        s for s in _applied_kind(cluster, "Secret") if s["metadata"]["name"] == "api-team-env"
    ]
    assert applied_env, "expected the env secret to be re-applied"
    assert base64.b64decode(applied_env[0]["data"]["API_KEY"]).decode() == "stored-secret"


async def test_container_update_keeps_creds_rekeyed_to_new_image_registry():
    import base64
    import json

    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.manifests.secrets import build_pull_secret

    # Existing container pulls from reg-a with stored creds bob/s3cret.
    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg-a.example.com/team/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        pull_secret="api-team-pull",
    )
    pull = build_pull_secret("api-team-pull", {}, "reg-a.example.com", "bob", "s3cret")
    cluster = _ApplyCluster("site-a", {"api-team": existing}, secrets={"api-team-pull": pull})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # Move the image to a DIFFERENT registry, keeping the creds: echo the shown
    # username back (no token) -> keep, which re-keys to the new registry.
    await csvc.update(
        "team",
        "api",
        ContainerUpdate(image="reg-b.example.com/team/app:2", port=8080, registryUsername="bob"),
        user,
    )
    applied_pull = [
        s for s in _applied_kind(cluster, "Secret") if s["metadata"]["name"] == "api-team-pull"
    ]
    assert applied_pull, "expected the pull secret to be re-materialized on keep"
    cfg = json.loads(base64.b64decode(applied_pull[0]["data"][".dockerconfigjson"]))
    # re-keyed to the new image's registry, carrying the stored creds forward
    assert set(cfg["auths"]) == {"reg-b.example.com"}
    assert cfg["auths"]["reg-b.example.com"]["username"] == "bob"
    assert cfg["auths"]["reg-b.example.com"]["password"] == "s3cret"


async def test_update_container_username_change_without_token_rejected():
    import pytest
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.common import Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.manifests.secrets import build_pull_secret
    from common.errors import ValidationError

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg-a.example.com/team/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        pull_secret="api-team-pull",
    )
    pull = build_pull_secret("api-team-pull", {}, "reg-a.example.com", "bob", "s3cret")
    cluster = _ApplyCluster("site-a", {"api-team": existing}, secrets={"api-team-pull": pull})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    # A different username with no token can't rotate the credential -> synchronous 400.
    with pytest.raises(ValidationError):
        await csvc.accept_update(
            "team",
            "api",
            ContainerUpdate(image="reg/x:1", port=8080, registryUsername="alice"),
            user,
            BackgroundTasks(),
        )
    # Echoing the SAME username back (a keep) is accepted (202/Pending).
    resp = await csvc.accept_update(
        "team",
        "api",
        ContainerUpdate(image="reg/x:1", port=8080, registryUsername="bob"),
        user,
        BackgroundTasks(),
    )
    assert resp.overallStatus == "Pending"


async def test_container_update_both_creds_null_removes_pull_secret():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc
    from api.services.manifests.secrets import build_pull_secret
    from api.services.state.describe import pull_secret_name
    from common.cluster import ResourceKind

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg-a.example.com/team/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        pull_secret="api-team-pull",
    )
    pull = build_pull_secret("api-team-pull", {}, "reg-a.example.com", "bob", "s3cret")
    cluster = _ApplyCluster("site-a", {"api-team": existing}, secrets={"api-team-pull": pull})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    # Neither cred sent -> drop the pull secret and treat the image as public.
    await csvc.update("team", "api", ContainerUpdate(image="reg/x:1", port=8080), user)

    # the stale pull secret is pruned...
    assert (ResourceKind.SECRET, "api-team-pull") in cluster.deleted
    # ...and the re-applied KSVC references no pull secret
    ksvc = _applied_kind(cluster, "Service")[0]
    assert pull_secret_name(ksvc) is None
    # nothing re-applied it
    assert not [
        s for s in _applied_kind(cluster, "Secret") if s["metadata"]["name"] == "api-team-pull"
    ]


async def test_load_existing_surfaces_transient_secret_read_as_503():
    import pytest
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.cluster import ResourceKind
    from common.errors import NotFoundError, ServiceUnavailableError

    ksvc = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/x:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )

    class _FlakySecretCluster:
        site = "site-a"
        name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE and name == "api-team":
                return ksvc
            if kind == ResourceKind.SECRET:
                raise RuntimeError("etcd read timeout")  # transient, not a 404
            raise NotFoundError("nf")

    engine = _workload_service({"site-a": _FlakySecretCluster()})
    user = Principal(subject="u", username="alice", groups=["team"])
    # A transient failure reading the backing Secret must NOT look like "no stored
    # value" (which would 400 a valid keep) - it surfaces as a retryable 503.
    with pytest.raises(ServiceUnavailableError):
        await engine.load_existing("api", CONTAINER, user, "team")


async def test_load_existing_reads_secrets_from_local_site():
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests import resources as res
    from api.services.manifests.ksvc import build_ksvc

    ksvc = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/x:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    # Both sites have the workload but with different stored secret values; the read
    # must come from the local site (the cheapest, most reliable hop).
    local = _ApplyCluster(
        "site-a",
        {"api-team": ksvc},
        secrets={"api-team-env": res.build_secret("api-team-env", {}, {"K": "local-val"})},
    )
    remote = _ApplyCluster(
        "site-b",
        {"api-team": ksvc},
        secrets={"api-team-env": res.build_secret("api-team-env", {}, {"K": "remote-val"})},
    )
    engine = _workload_service({"site-a": local, "site-b": remote}, local_site="site-a")
    user = Principal(subject="u", username="alice", groups=["team"])

    state = await engine.load_existing("api", CONTAINER, user, "team")
    assert state["env_values"] == {"K": "local-val"}


async def test_list_fans_out_and_merges_sites():
    from cloudlet_apis.auth import Principal

    # orders is deployed to both sites; web only to site-a.
    site_a = _ListCluster(
        "site-a",
        [
            _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
            _list_ksvc("web-team", "small", "web-team.ex.com"),
        ],
    )
    site_b = _ListCluster(
        "site-b",
        [
            _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
        ],
    )
    engine = _workload_service({"site-a": site_a, "site-b": site_b})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list(CONTAINER, user, "team")
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
    from cloudlet_apis.auth import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    # site-a answers, site-b is down -> merge what site-a returned, don't fail.
    site_a = _ListCluster(
        "site-a",
        [
            _list_ksvc("orders-team", "medium", "orders-team.ex.com"),
        ],
    )
    engine = _workload_service({"site-a": site_a, "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])

    out = await engine.list(CONTAINER, user, "team")
    assert [w.name for w in out] == ["orders"]
    assert out[0].sites == ["site-a"]  # only the reachable site contributes


async def test_list_sort_by_created_at():
    from cloudlet_apis.auth import Principal

    def _with_created(oname, created):
        k = _list_ksvc(oname, "small", f"{oname}.ex.com")
        k["metadata"]["creationTimestamp"] = created
        return k

    local = _ListCluster(
        "site-a",
        [
            _with_created("aaa-team", "2026-01-02T00:00:00Z"),  # name first, created newer
            _with_created("bbb-team", "2026-01-01T00:00:00Z"),  # name last, created older
        ],
    )
    engine = _workload_service({"site-a": local}, local_site="site-a")
    user = Principal(subject="u", username="alice", groups=["team"])

    by_name = await engine.list(CONTAINER, user, "team")  # default
    assert [w.name for w in by_name] == ["aaa", "bbb"]
    assert by_name[0].createdAt is not None  # creation time populated

    by_created = await engine.list(CONTAINER, user, "team", sort="createdAt")
    assert [w.name for w in by_created] == ["bbb", "aaa"]  # oldest first


async def test_list_errors_when_all_sites_fail():
    from cloudlet_apis.auth import Principal

    from common.errors import SiteTotalFailure

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
        await engine.list(CONTAINER, user, "team")


async def test_accept_rejects_group_caller_is_not_member_of():
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService
    from common.errors import ForbiddenError

    engine = _workload_service({"site-a": _FakeCluster("site-a")})
    svc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])  # not 'other'
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )
    with pytest.raises(ForbiddenError):  # 403 before anything is scheduled
        await svc.accept("other", spec, user, BackgroundTasks())


class _DeleteCluster:
    """Records deletions; serves a preset KSVC (or none) by name."""

    def __init__(self, name, ksvc):
        self.name = name
        self.site = name
        self._ksvc = ksvc  # the KSVC dict, or None if absent
        self.deleted = []  # [(ResourceKind, name)]

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from common.cluster import ResourceKind
        from common.errors import NotFoundError as _NF

        if kind == ResourceKind.KNATIVE_SERVICE and self._ksvc is not None:
            return self._ksvc
        raise _NF(f"{name} not found")

    def delete(self, kind, name):
        self.deleted.append((kind, name))


async def test_delete_removes_ksvc_and_relies_on_gc():
    """Delete removes only the KSVC; the derived resources are garbage-collected
    by Kubernetes via their ownerReferences (set at apply time - see
    test_apply_sets_owner_references_on_derived)."""
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind

    cluster = _DeleteCluster("site-a", _ksvc("container"))
    engine = _workload_service({"site-a": cluster})
    user = Principal(subject="u", username="alice", groups=["team"])

    await engine.delete(CONTAINER, "app", user, "team")

    assert cluster.deleted == [(ResourceKind.KNATIVE_SERVICE, "app-team")]


async def test_delete_missing_workload_is_404():
    from cloudlet_apis.auth import Principal

    from common.errors import NotFoundError

    cluster = _DeleteCluster("site-a", None)  # no KSVC present
    engine = _workload_service({"site-a": cluster})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await engine.delete(CONTAINER, "app", user, "team")
    assert cluster.deleted == []  # nothing deleted when the workload is absent


async def test_delete_fails_closed_when_a_site_cannot_be_reached():
    """An unreachable site cannot prove the workload is gone.

    Reporting 404 there would read as "already deleted" while the workload is
    still serving on the site that did not answer - and would do so *after*
    tearing down the build objects, leaving a running function that can never
    rebuild. Every other cross-site check in the engine fails closed with 503;
    delete has to as well.
    """
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind
    from common.errors import ServiceUnavailableError

    up = _DeleteCluster("site-a", _ksvc("function"))
    down = _DownCluster()
    down.site = down.name = "site-b"
    engine = _workload_service({"site-a": up, "site-b": down}, builder=_NullBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(ServiceUnavailableError):
        await engine.delete(FUNCTION, "app", user, "team")

    # the build objects survive: nothing may be torn down on an unconfirmed delete
    assert not any(kind != ResourceKind.KNATIVE_SERVICE for kind, _ in up.deleted)


async def test_delete_reaps_orphaned_build_objects_once_every_site_answers():
    """A conclusive "gone everywhere" is what licenses the build-object cleanup.

    The KSVC being absent does not mean the build objects are: a partial delete
    can orphan them, and a leftover Image keeps rebuilding a function nothing
    runs. So the reap happens whenever the fan-out is conclusive, then the 404 is
    reported for the workload itself.
    """
    from cloudlet_apis.auth import Principal

    from common.cluster import ResourceKind
    from common.errors import NotFoundError

    cluster = _DeleteCluster("site-a", None)  # KSVC absent, build objects orphaned
    engine = _workload_service({"site-a": cluster}, builder=_NullBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(NotFoundError):
        await engine.delete(FUNCTION, "app", user, "team")

    assert (ResourceKind.KPACK_IMAGE, "fn-app-team") in cluster.deleted
    assert (ResourceKind.SERVICE_ACCOUNT, "fn-app-team") in cluster.deleted
    assert (ResourceKind.SECRET, "app-team-git") in cluster.deleted


async def test_delete_of_another_groups_workload_is_404_and_deletes_nothing():
    """An object_name collision resolving to another group must not delete anything.

    The refusal is recorded per site rather than raised inside the fan-out, which
    would swallow it into a per-site error string and make it indistinguishable
    from an unreachable site.
    """
    from cloudlet_apis.auth import Principal

    from api.models.common import LABEL_GROUP, LABEL_OFFERING
    from common.errors import NotFoundError

    foreign = _ksvc("function")
    foreign["metadata"]["labels"] = {LABEL_GROUP: "other", LABEL_OFFERING: "function"}
    cluster = _DeleteCluster("site-a", foreign)
    engine = _workload_service({"site-a": cluster}, builder=_NullBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(NotFoundError):
        await engine.delete(FUNCTION, "app", user, "team")
    assert cluster.deleted == []  # not the KSVC, and not the build objects


async def test_deployable_check_reports_host_and_name_conflicts_in_one_pass():
    """Host and name are answered in a single visit per site.

    Two separate fan-outs cost two cross-site round trips and described two
    different instants. Merging them must not weaken either verdict, so both
    conflicts and the fail-closed behaviour are pinned here.
    """
    from api.models.common import LABEL_WORKLOAD
    from common.errors import ConflictError, ServiceUnavailableError

    host = "app-team.serverless.example.com"

    # host held by someone else -> conflict, even though the name is free
    taken = {host: {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "other-team"}}}}
    svc = _workload_service({"site-a": _FakeCluster("site-a", existing=taken)})
    with pytest.raises(ConflictError, match="hostname"):
        await svc.assert_deployable(
            "app", "team", svc.deployer.resolve_targets(None), host=host, require_absent=True
        )

    # name already used -> conflict, even though the host is free
    exists = {"app-team": {"metadata": {"name": "app-team"}}}
    svc = _workload_service({"site-a": _FakeCluster("site-a", existing=exists)})
    with pytest.raises(ConflictError, match="already exists"):
        await svc.assert_deployable(
            "app", "team", svc.deployer.resolve_targets(None), host=host, require_absent=True
        )

    # an update does not require absence: its own KSVC and mapping are expected
    own = {
        "app-team": {"metadata": {"name": "app-team"}},
        host: {"metadata": {"name": host, "labels": {LABEL_WORKLOAD: "app-team"}}},
    }
    svc = _workload_service({"site-a": _FakeCluster("site-a", existing=own)})
    await svc.assert_deployable("app", "team", svc.deployer.resolve_targets(None), host=host)

    # a site that cannot answer proves neither -> 503, not a silent pass
    svc = _workload_service({"site-a": _DownCluster()})
    with pytest.raises(ServiceUnavailableError):
        await svc.assert_deployable(
            "app", "team", svc.deployer.resolve_targets(None), host=host, require_absent=True
        )


async def test_apply_sets_owner_references_on_derived():
    """Every resource derived from a workload (env/files Secret & ConfigMap, the
    imagePullSecret, and the DomainMapping) must carry an ownerReference to the
    KSVC so Kubernetes GC cleans them up on delete. The KSVC itself owns nothing."""
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar, FileMount, Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg.acme.com/api:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    engine = _workload_service({"site-a": cluster})
    csvc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.update(
        "team",
        "api",
        ContainerUpdate(
            image="reg/x:1",
            port=8080,
            registryUsername="bob",
            registryToken="t",  # -> pull Secret
            env=[EnvVar(name="PW", value="x", secret=True)],  # -> env Secret
            files=[FileMount(mountPath="/etc/app/c", content="hi")],  # -> files ConfigMap
        ),
        user,
    )

    ksvc = _applied_kind(cluster, "Service")[0]
    assert "ownerReferences" not in ksvc["metadata"]  # the owner owns nothing

    derived = [
        m for m in cluster.applied if m.get("kind") in ("Secret", "ConfigMap", "DomainMapping")
    ]
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
    from common.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})
    with pytest.raises(ServiceUnavailableError):
        await svc.assert_host_available(
            "h.example.com", "app", "team", svc.deployer.resolve_targets(None)
        )


async def test_absent_check_fails_closed_when_site_unreachable():
    from common.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})
    with pytest.raises(ServiceUnavailableError):
        await svc.assert_workload_absent("app", "team", svc.deployer.resolve_targets(None))


async def test_host_check_still_available_on_real_404():
    # A genuine NotFound (404) still reads as available across a reachable site.
    svc = _workload_service({"site-a": _FakeCluster("site-a")})  # get -> NotFoundError
    await svc.assert_host_available(
        "h.example.com", "app", "team", svc.deployer.resolve_targets(None)
    )


async def test_accept_rejects_invalid_spec_synchronously():
    # A malformed spec must 400 at accept time, before anything is scheduled.
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.common import FileMount
    from api.models.container import ContainerCreate
    from api.services.container import ContainerService
    from common.errors import ValidationError

    engine = _workload_service({"site-a": _DownCluster()})  # would error if reached
    svc = ContainerService(engine)
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    spec = ContainerCreate(
        name="app",
        image="reg/x:1",
        port=8080,
        files=[
            FileMount(mountPath="/a/conf", content="1"),
            FileMount(mountPath="/a/conf", content="2"),
        ],  # duplicate key
    )
    with pytest.raises(ValidationError):
        await svc.accept("team", spec, user, bg)
    assert bg.tasks == []  # validation ran before the background deploy was queued


async def test_update_reuses_existing_without_refetch():
    # accept_update passes the loaded `existing` into update(); update must reuse
    # it and NOT trigger a second load_existing fanout.
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc

    existing_ksvc = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
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
    await csvc.update(
        "team", "api", ContainerUpdate(image="reg/x:1", port=8080), user, existing=preloaded
    )
    assert calls["n"] == 0  # reused the passed-in existing, no second fanout


async def test_load_existing_unreachable_site_is_503_not_404():
    # A workload that can't be confirmed absent because the site is down must
    # surface ServiceUnavailable, not a misleading NotFound.
    from cloudlet_apis.auth import Principal

    from common.errors import ServiceUnavailableError

    svc = _workload_service({"site-a": _DownCluster()})  # get() raises RuntimeError
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(ServiceUnavailableError):
        await svc.load_existing("app", CONTAINER, user, "team")


async def test_load_existing_truly_absent_is_404():
    from cloudlet_apis.auth import Principal

    from common.errors import NotFoundError

    svc = _workload_service({"site-a": _FakeCluster("site-a")})  # get() -> NotFoundError
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await svc.load_existing("app", CONTAINER, user, "team")


def _ready_ksvc(name="app-team"):
    k = _bare_ksvc(name)
    k["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    return k


async def test_get_omits_404_site_and_stays_healthy():
    # A site that returns a clean 404 (workload not deployed there) is omitted from
    # the per-site report and does NOT drag the rollup to Degraded.
    from cloudlet_apis.auth import Principal

    engine = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ready_ksvc()}),
            "site-b": _FakeCluster("site-b"),  # 404 here
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.overallStatus == "Ready"
    assert [s.site for s in body.sites] == ["site-a"]  # site-b omitted, not Failed


async def test_get_down_site_still_degrades():
    # An UNREACHABLE site is different from a 404: it stays visible and degrades.
    from cloudlet_apis.auth import Principal

    engine = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": _ready_ksvc()}),
            "site-b": _DownCluster("site-b"),  # unreachable
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.overallStatus == "Degraded"
    assert {s.site for s in body.sites} == {"site-a", "site-b"}


async def test_get_absent_everywhere_is_404():
    from cloudlet_apis.auth import Principal

    from common.errors import NotFoundError

    engine = _workload_service({"site-a": _FakeCluster("site-a"), "site-b": _FakeCluster("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(NotFoundError):
        await engine.get(CONTAINER, "app", user, "team")


async def test_get_all_sites_down_is_503():
    # Can't confirm absence when every site is unreachable -> 503, not a false 404.
    from cloudlet_apis.auth import Principal

    from common.errors import ServiceUnavailableError

    engine = _workload_service({"site-a": _DownCluster("site-a"), "site-b": _DownCluster("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(ServiceUnavailableError):
        await engine.get(CONTAINER, "app", user, "team")


# --- Review fixes: fail-closed prune (A1/A3), client cleanup (B1), guards (A2, D1) ---


def _existing_container_ksvc():
    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc

    return build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="api-team.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )


async def test_prune_failure_aborts_update_fail_closed():
    """A non-404 prune error must abort the site's update: the new spec never goes
    live, so it can't sit alongside the stale, now-unreferenced secret."""
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService

    class _PruneFails(_ApplyCluster):
        def delete(self, kind, name):  # a real API error, not a 404
            raise RuntimeError("boom: API error, not a 404")

    cluster = _PruneFails("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):  # single site failed -> total failure
        await csvc.update(
            "team",
            "api",
            ContainerUpdate(image="reg/x:1", port=8080, env=[EnvVar(name="LOG", value="debug")]),
            user,
        )
    # fail-closed: the KSVC was never (re)applied after the prune failed
    assert _applied_kind(cluster, "Service") == []


async def test_prune_not_found_is_tolerated():
    """A 404 during prune just means the object never existed here; the update
    proceeds normally."""
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from common.errors import NotFoundError

    class _PruneMissing(_ApplyCluster):
        def delete(self, kind, name):
            self.deleted.append((kind, name))
            raise NotFoundError("already gone")

    cluster = _PruneMissing("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    await csvc.update(  # must not raise
        "team",
        "api",
        ContainerUpdate(image="reg/x:1", port=8080, env=[EnvVar(name="LOG", value="debug")]),
        user,
    )
    assert _applied_kind(cluster, "Service")  # KSVC applied -> update proceeded


async def test_prune_runs_before_apply_on_update():
    """Ordering: the fail-closed secret/configmap prunes happen before the new
    spec is applied, while the old host's DomainMapping is retired *after* (prune-
    last) so a host move never drops the old host before the new one is live."""
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from common.cluster import ResourceKind

    class _OpLog(_ApplyCluster):
        def __init__(self, name, existing):
            super().__init__(name, existing)
            self.ops = []  # [(op, kind)]

        def apply(self, manifest):
            self.ops.append(("apply", manifest.get("kind")))
            return super().apply(manifest)

        def delete(self, kind, name):
            self.ops.append(("delete", kind))
            return super().delete(kind, name)

    cluster = _OpLog("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    # The fixture KSVC sits on a custom host; an update with no hostname resolves
    # to the default host -> the host changes, exercising the old-host retirement.
    await csvc.update(
        "team",
        "api",
        ContainerUpdate(image="reg/x:1", port=8080, env=[EnvVar(name="LOG", value="debug")]),
        user,
    )
    first_apply = next(i for i, (op, _) in enumerate(cluster.ops) if op == "apply")
    # Every fail-closed prune (stale env/files Secret & ConfigMap) precedes apply.
    prune_deletes = [
        i
        for i, (op, kind) in enumerate(cluster.ops)
        if op == "delete" and kind != ResourceKind.DOMAIN_MAPPING
    ]
    assert prune_deletes and all(i < first_apply for i in prune_deletes)
    # The old host's DomainMapping is retired last - after the new mapping is live.
    dm_deletes = [
        i
        for i, (op, kind) in enumerate(cluster.ops)
        if op == "delete" and kind == ResourceKind.DOMAIN_MAPPING
    ]
    assert dm_deletes and all(i > first_apply for i in dm_deletes)


async def test_accept_update_rejects_taken_host_synchronously():
    """A host already owned by another workload must 409 at accept time, not be
    swallowed in the background deploy where the client would never see it."""
    from cloudlet_apis.auth import Principal
    from fastapi import BackgroundTasks

    from api.models.common import LABEL_WORKLOAD
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from common.cluster import ResourceKind
    from common.errors import ConflictError, NotFoundError

    existing = _existing_container_ksvc()

    class _HostTaken:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE and name == "api-team":
                return existing
            if kind == ResourceKind.DOMAIN_MAPPING and name == "shop.serverless.example.com":
                # owned by a DIFFERENT workload -> the host is taken
                return {"metadata": {"name": name, "labels": {LABEL_WORKLOAD: "shop-team"}}}
            raise NotFoundError("not found")

    csvc = ContainerService(_workload_service({"site-a": _HostTaken("site-a")}))
    user = Principal(subject="u", username="alice", groups=["team"])
    bg = BackgroundTasks()
    with pytest.raises(ConflictError):
        await csvc.accept_update(
            "team", "api", ContainerUpdate(image="reg/x:1", port=8080, hostname="shop"), user, bg
        )
    assert bg.tasks == []  # rejected before the background deploy was queued


async def test_update_unchanged_host_retires_no_mapping():
    """When the host is unchanged, no DomainMapping is retired (no spurious delete)."""
    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from api.services.manifests.ksvc import build_ksvc
    from common.cluster import ResourceKind

    existing = build_ksvc(
        name="api-team",
        group="team",
        owner="alice",
        image="reg/app:1",
        offering="container",
        host="api-team.serverless.example.com",  # the default host
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    cluster = _ApplyCluster("site-a", {"api-team": existing})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    # no hostname -> default host
    await csvc.update("team", "api", ContainerUpdate(image="reg/x:1", port=8080), user)

    dm_deletes = [n for k, n in cluster.deleted if k == ResourceKind.DOMAIN_MAPPING]
    assert dm_deletes == []  # host unchanged -> nothing retired


async def test_list_total_failure_details_have_message_key():
    """The list total-failure details share deployer.aggregate's {site, message}
    shape so clients parse one envelope."""
    from cloudlet_apis.auth import Principal

    class _Boom:
        def __init__(self, name):
            self.site = name
            self.name = name

        def get(self, *a, **k):
            raise RuntimeError("site down")

    engine = _workload_service({"site-a": _Boom("site-a"), "site-b": _Boom("site-b")})
    user = Principal(subject="u", username="alice", groups=["team"])
    with pytest.raises(SiteTotalFailure) as ei:
        await engine.list(CONTAINER, user, "team")
    assert all(set(d) == {"site", "message"} for d in ei.value.details)


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
    from common.cluster import Cluster

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
    from cloudlet_apis.auth import Principal

    terminating = _ready_ksvc()  # would otherwise read Ready
    terminating["metadata"]["deletionTimestamp"] = "2026-06-29T12:00:00Z"
    engine = _workload_service(
        {
            "site-a": _FakeCluster("site-a", existing={"app-team": terminating}),
            "site-b": _FakeCluster("site-b"),  # 404 here
        }
    )
    user = Principal(subject="u", username="alice", groups=["team"])
    body = await engine.get(CONTAINER, "app", user, "team")
    assert body.overallStatus == "Terminating"
    assert body.sites[0].status == "Terminating"


def test_overall_status_terminating_precedence():
    from api.services.sites.deployer import overall_status

    assert overall_status(["Terminating", "Ready"]) == "Terminating"
    assert overall_status(["Terminating", "Deploying"]) == "Terminating"
    # A real failure still outranks a termination in progress.
    assert overall_status(["Failed", "Terminating"]) == "Degraded"


def test_creation_time_is_israel_local_time_with_dst():
    """metadata.creationTimestamp (UTC) is surfaced in Israel local time, with the
    DST-aware offset: +03:00 (IDT) in summer, +02:00 (IST) in winter."""
    from zoneinfo import ZoneInfo

    from api.services.state.ksvc_state import creation_time

    summer = creation_time({"metadata": {"creationTimestamp": "2026-07-01T09:00:00Z"}})
    assert summer.tzinfo == ZoneInfo("Asia/Jerusalem")
    assert summer.utcoffset().total_seconds() == 3 * 3600  # IDT
    assert (summer.hour, summer.minute) == (12, 0)  # 09:00Z -> 12:00 IDT

    winter = creation_time({"metadata": {"creationTimestamp": "2026-01-01T09:00:00Z"}})
    assert winter.utcoffset().total_seconds() == 2 * 3600  # IST
    assert winter.hour == 11  # 09:00Z -> 11:00 IST

    assert creation_time({"metadata": {}}) is None  # absent -> None


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
    from cloudlet_apis.auth import Principal

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService
    from common.cluster import ResourceKind

    cluster = _BackingFails("site-a", {})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):  # single site failed
        await csvc.create("team", ContainerCreate(name="api", image="reg/x:1", port=8080), user)
    assert (ResourceKind.KNATIVE_SERVICE, "api-team") in cluster.deleted


async def test_update_does_not_roll_back_live_ksvc_on_backing_failure():
    """A backing/mapping failure on update must NOT delete the live KSVC - Knative
    keeps serving the last-good revision; deleting would take it down + free the host."""
    from cloudlet_apis.auth import Principal

    from api.models.common import EnvVar
    from api.models.container import ContainerUpdate
    from api.services.container import ContainerService
    from common.cluster import ResourceKind

    cluster = _BackingFails("site-a", {"api-team": _existing_container_ksvc()})
    csvc = ContainerService(_workload_service({"site-a": cluster}))
    user = Principal(subject="u", username="alice", groups=["team"])

    with pytest.raises(SiteTotalFailure):
        await csvc.update(
            "team",
            "api",
            ContainerUpdate(image="reg/x:1", port=8080, env=[EnvVar(name="LOG", value="debug")]),
            user,
        )
    assert (ResourceKind.KNATIVE_SERVICE, "api-team") not in cluster.deleted


# --- runtime validation against the registry (config-driven) ---------------


def _function_service_with_runtimes(names, builder="python"):
    from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec
    from api.services.function import FunctionService

    engine = _workload_service({"site-a": _FakeCluster("site-a")})
    # `builder` is what makes a runtime buildable; pass None for one that is
    # advertised but unusable (the shape of an unmounted runtimes ConfigMap).
    registry = RuntimeRegistry([RuntimeSpec(name=n, builder=builder) for n in names])
    return FunctionService(engine, registry)


async def test_function_accept_rejects_a_runtime_with_no_builder():
    """An unmounted runtimes ConfigMap must 400 now, not fail the deploy later."""
    from cloudlet_apis.auth import Principal
    from starlette.background import BackgroundTasks

    from api.models.function import FunctionCreate
    from common.errors import ValidationError

    fsvc = _function_service_with_runtimes(["python"], builder=None)
    user = Principal(subject="u", username="alice", groups=["team"])
    spec = FunctionCreate(
        name="fn", gitRepo="https://git.internal/o/r.git", gitToken="t", runtime="python"
    )

    with pytest.raises(ValidationError, match="not buildable"):
        await fsvc.accept("team", spec, user, BackgroundTasks())


async def test_function_accept_rejects_unknown_runtime():
    from cloudlet_apis.auth import Principal
    from starlette.background import BackgroundTasks

    from api.models.function import FunctionCreate
    from common.errors import ValidationError

    fsvc = _function_service_with_runtimes(["python", "go"])
    user = Principal(subject="u", username="alice", groups=["team"])
    spec = FunctionCreate(
        name="fn", gitRepo="https://git.internal/o/r.git", gitToken="t", runtime="ruby"
    )

    with pytest.raises(ValidationError):
        await fsvc.accept("team", spec, user, BackgroundTasks())


async def test_function_accept_allows_known_runtime():
    from cloudlet_apis.auth import Principal
    from starlette.background import BackgroundTasks

    from api.models.function import FunctionCreate

    fsvc = _function_service_with_runtimes(["python", "go"])
    user = Principal(subject="u", username="alice", groups=["team"])
    spec = FunctionCreate(
        name="fn", gitRepo="https://git.internal/o/r.git", gitToken="t", runtime="go"
    )

    resp = await fsvc.accept("team", spec, user, BackgroundTasks())
    assert resp.overallStatus == "Pending" and resp.runtime == "go"


async def test_function_update_rejects_unknown_runtime():
    from cloudlet_apis.auth import Principal
    from starlette.background import BackgroundTasks

    from api.models.function import FunctionUpdate
    from common.errors import ValidationError

    fsvc = _function_service_with_runtimes(["python"])
    user = Principal(subject="u", username="alice", groups=["team"])
    # an unknown runtime is rejected up front, before any build/token logic
    spec = FunctionUpdate(gitRepo="https://git/x.git", runtime="ruby", gitToken="t")

    with pytest.raises(ValidationError):
        await fsvc.accept_update("team", "fn", spec, user, BackgroundTasks())


async def test_apply_takes_status_from_the_apply_response_not_a_second_read():
    """The apply path must not re-read the KSVC it just wrote.

    Server-side apply returns the stored object - already trusted enough to
    source the ownerReference every derived resource hangs off - and Knative has
    not reconciled microseconds later, so a follow-up GET buys nothing and costs
    a cross-site round trip on every site of every deploy.
    """
    from cloudlet_apis.auth import Principal

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService
    from common.cluster import ResourceKind

    class _CountingApply(_ApplyCluster):
        ksvc_gets = 0

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                type(self).ksvc_gets += 1
            return super().get(kind, name, label_selector, namespace)

    _CountingApply.ksvc_gets = 0
    cluster = _CountingApply("site-a", {})
    engine = _workload_service({"site-a": cluster})
    user = Principal(subject="u", username="alice", groups=["team"])

    body, _ = await ContainerService(engine).create(
        "team", ContainerCreate(name="api", image="reg/x:1", port=8080), user
    )

    # exactly one: the pre-flight absence probe. The post-apply read-back is gone.
    assert _CountingApply.ksvc_gets == 1
    # and the status still comes back, sourced from the apply response
    assert body.sites[0].status == "Deploying"  # just written, not yet reconciled


def _thread_recording_cluster(seen):
    import threading

    from common.cluster import ResourceKind

    ksvc = _bare_ksvc()
    # a revision name is what makes the Revision read happen at all
    ksvc["status"] = {
        "conditions": [{"type": "Ready", "status": "True"}],
        "latestReadyRevisionName": "app-team-00001",
    }

    class _ThreadRecordingCluster:
        site = name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            here = threading.current_thread().name
            if kind == ResourceKind.KNATIVE_SERVICE:
                seen["ksvc"] = here
                return ksvc
            if kind == ResourceKind.KNATIVE_REVISION:
                seen["revision"] = here
                return {"status": {"actualReplicas": 2}}
            if kind == ResourceKind.POD_METRICS:
                seen["usage"] = here
                return []
            raise AssertionError(f"unexpected kind {kind}")

    return _ThreadRecordingCluster()


async def test_get_reads_a_sites_revision_on_the_fanout_thread_and_never_measures():
    """No per-site, per-request thread pool - and no metrics call at all.

    The Revision read used to run in a ThreadPoolExecutor constructed inside the
    fan-out worker - nesting a pool inside the default executor the worker was
    itself borrowed from. Running it on the calling thread is what keeps a poll
    loop from filling the outer pool with threads that only wait.

    The usage read is no longer here at all: measuring costs a cluster call, and
    the full GET is not the endpoint to poll.
    """
    from cloudlet_apis.auth import Principal

    seen: dict[str, str] = {}
    engine = _workload_service({"site-a": _thread_recording_cluster(seen)})
    user = Principal(subject="u", username="alice", groups=["team"])
    await engine.get(CONTAINER, "app", user, "team")

    assert {"ksvc", "revision"} <= seen.keys()
    assert seen["revision"] == seen["ksvc"], "revision read spawned a thread"
    assert "usage" not in seen, "the full GET measured usage; that belongs to /stats"


async def test_stats_reads_revision_and_usage_on_the_fanout_thread():
    """The stats view takes the measurement, and on the same thread."""
    from cloudlet_apis.auth import Principal

    seen: dict[str, str] = {}
    engine = _workload_service({"site-a": _thread_recording_cluster(seen)})
    user = Principal(subject="u", username="alice", groups=["team"])
    await engine.stats(CONTAINER, "app", user, "team")

    assert {"ksvc", "revision", "usage"} <= seen.keys()
    assert seen["revision"] == seen["ksvc"], "revision read spawned a thread"
    assert seen["usage"] == seen["ksvc"], "usage read spawned a thread"


async def test_get_overlaps_the_spec_and_build_reads():
    """The spec read and the build read are independent, so they must overlap.

    Asserted as overlapping intervals rather than elapsed time, so the result
    does not depend on how fast or loaded the machine is: run in sequence the
    two windows cannot intersect, run concurrently they must.
    """
    import time

    from cloudlet_apis.auth import Principal

    from api.models.common import Scaling
    from api.services.manifests.files import VolumeSpec
    from api.services.manifests.ksvc import build_ksvc
    from common.build import BuildStatus
    from common.cluster import ResourceKind

    # a function carrying one mounted file, so reading its spec costs a ConfigMap get
    ksvc = build_ksvc(
        name="app-team",
        group="team",
        owner="alice",
        image="reg/app:main",
        offering="function",
        host="app-team.ex.com",
        env=[],
        volumes=[
            VolumeSpec("files-config", "configmap", "app-team-files", "/etc/a.conf", "etc-a.conf")
        ],
        scaling=Scaling(),
        size="small",
        runtime="python",
    )
    ksvc["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}

    spans: dict[str, list[float]] = {}

    def _record(key):
        spans[key] = [time.monotonic()]
        time.sleep(0.05)
        spans[key].append(time.monotonic())

    class _SpecCluster:
        site = name = "site-a"

        def get(self, kind, name=None, label_selector=None, namespace=None):
            if kind == ResourceKind.KNATIVE_SERVICE:
                return ksvc
            if kind == ResourceKind.CONFIG_MAP:
                _record("spec")
                return {"data": {}}
            from common.errors import NotFoundError as _NF

            raise _NF("best-effort")

    class _SlowBuilder(_NullBuilder):
        def status(self, cluster, name, group):
            _record("build")
            return BuildStatus(state="Ready")

    engine = _workload_service({"site-a": _SpecCluster()}, builder=_SlowBuilder())
    user = Principal(subject="u", username="alice", groups=["team"])
    await engine.get(FUNCTION, "app", user, "team")

    (spec_start, spec_end), (build_start, build_end) = spans["spec"], spans["build"]
    assert spec_start < build_end and build_start < spec_end, "reads did not overlap"
