"""kpack build path: manifest shapes, the builder, and build-aware status."""

from __future__ import annotations

import base64

import pytest

from api.services import kpack
from api.services.builder import KpackBuilder
from api.services.deployer import Deployer
from api.services.runtimes import RuntimeRegistry, RuntimeSpec
from common.config import CommonSettings, SiteConfig
from common.errors import NotFoundError, ValidationError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Cluster:
    """Records applies/deletes per namespace; serves preset objects."""

    def __init__(self, name="site-a", objects=None):
        self.site = name
        self.name = name
        self._objects = objects or {}  # (kind, name) -> obj
        self.applied = []  # [(namespace, manifest)]
        self.deleted = []  # [(namespace, kind, name)]

    def get(self, kind, name=None, label_selector=None, namespace=None):
        try:
            return self._objects[(kind, name)]
        except KeyError:
            raise NotFoundError(f"{name} not found") from None

    def apply(self, manifest, namespace=None):
        self.applied.append((namespace, manifest))
        return [manifest]

    def delete(self, kind, name, namespace=None):
        if (kind, name) not in self._objects:
            raise NotFoundError(f"{name} not found")
        self.deleted.append((namespace, kind, name))


def _settings(**over):
    base = dict(
        sites=[SiteConfig(name="site-a", cluster="a-0")],
        workloads_namespace="wl",
        build={"namespace": "bld", "registry_secret": "reg-creds"},
        registry={"url": "registry.internal", "organization": "acme"},
    )
    base.update(over)
    return CommonSettings(**base)


def _runtimes():
    return RuntimeRegistry(
        [
            RuntimeSpec(
                name="python",
                builder="python",
                versionEnv="BP_CPYTHON_VERSION",
                defaultVersion="3.12",
                buildEnv=[{"name": "PIP_INDEX_URL", "value": "https://art/simple"}],
            ),
            RuntimeSpec(name="broken"),  # no builder -> must be rejected
        ]
    )


def _builder(cluster, settings=None):
    settings = settings or _settings()
    deployer = Deployer(settings)
    deployer._clusters = {"site-a": cluster}
    return KpackBuilder(settings, deployer, _runtimes())


def _request(**over):
    from common.contract import BuildRequest

    kwargs = dict(
        name="hello",
        group="payments",
        git_url="https://git.internal/payments/hello.git",
        branch="main",
        git_token="ghp_tok",
        runtime="python",
        owner="alice",
    )
    kwargs.update(over)
    return BuildRequest(**kwargs)


# --------------------------------------------------------------- manifests


def test_git_credential_host_strips_path_and_userinfo():
    assert kpack.git_credential_host("https://git.internal/team/app.git") == "https://git.internal"
    assert (
        kpack.git_credential_host("https://u:p@git.internal:8443/a") == "https://git.internal:8443"
    )
    # kpack needs a host; a bare path has none to give, so it passes through
    assert kpack.git_credential_host("git@host:team/app.git") == "git@host:team/app.git"


def test_git_secret_is_basic_auth_with_the_kpack_annotation():
    secret = kpack.build_git_secret(
        "fn-x-git", {"a": "b"}, "https://git.internal/t/a.git", "x-access-token", "ghp_tok"
    )
    # kpack ignores an Opaque secret and one without the annotation, so both matter
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["metadata"]["annotations"][kpack.GIT_ANNOTATION] == "https://git.internal"
    assert base64.b64decode(secret["data"]["password"]).decode() == "ghp_tok"
    assert base64.b64decode(secret["data"]["username"]).decode() == "x-access-token"


def test_service_account_carries_registry_in_both_lists():
    sa = kpack.build_service_account("fn-x", {}, "fn-x-git", "reg-creds")
    # `secrets` is what kpack pushes with; `imagePullSecrets` is the build pod's
    assert sa["secrets"] == [{"name": "fn-x-git"}, {"name": "reg-creds"}]
    assert sa["imagePullSecrets"] == [{"name": "reg-creds"}]


def test_image_never_sets_creation_time():
    image = kpack.build_image(
        "fn-x",
        {},
        tag="reg/x:main",
        builder="python",
        service_account="fn-x",
        git_url="https://git/x.git",
        revision="main",
    )
    # creationTime is a nonce: setting it makes every apply look like a change
    # and rebuilds forever under active/active.
    assert "creationTime" not in image["spec"]
    assert image["spec"]["builder"] == {"kind": "Builder", "name": "python"}
    assert "build" not in image["spec"]  # omitted when there is no env/resources


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "Unknown"),
        ({}, "Building"),  # applied, not yet reconciled
        ({"status": {"conditions": [{"type": "Ready", "status": "Unknown"}]}}, "Building"),
        ({"status": {"conditions": [{"type": "Ready", "status": "True"}]}}, "Ready"),
        ({"status": {"conditions": [{"type": "Ready", "status": "False"}]}}, "Failed"),
    ],
)
def test_build_status_reduction(status, expected):
    state, _, _ = kpack.build_status(status)
    assert state == expected


def test_build_status_keeps_last_image_through_a_failure():
    state, image, message = kpack.build_status(
        {
            "status": {
                "latestImage": "reg/x@sha256:old",
                "conditions": [{"type": "Ready", "status": "False", "message": "detect failed"}],
            }
        }
    )
    # a failed rebuild still reports the previous good image - the state, not the
    # presence of an image, says whether the function is current
    assert (state, image, message) == ("Failed", "reg/x@sha256:old", "detect failed")


# ----------------------------------------------------------------- builder


def test_build_applies_secret_account_and_image_into_the_build_namespace():
    cluster = _Cluster()
    result = _builder(cluster).build(_request())

    namespaces = {ns for ns, _ in cluster.applied}
    assert namespaces == {"bld"}, "build objects must not land in the workloads namespace"

    kinds = [m["kind"] for _, m in cluster.applied]
    # dependency order: the SA references the Secret, the Image references the SA
    assert kinds == ["Secret", "ServiceAccount", "Image"]

    image = cluster.applied[-1][1]
    assert image["metadata"]["name"] == "fn-hello-payments"
    assert image["spec"]["serviceAccountName"] == "fn-hello-payments"
    assert image["spec"]["source"]["git"] == {
        "url": "https://git.internal/payments/hello.git",
        "revision": "main",
    }
    assert result.image == "registry.internal/acme/payments/hello:main"
    assert result.digest is None, "the build has only been requested, not finished"


def test_build_env_merges_runtime_env_and_version():
    cluster = _Cluster()
    _builder(cluster).build(_request())
    env = cluster.applied[-1][1]["spec"]["build"]["env"]
    assert {"name": "PIP_INDEX_URL", "value": "https://art/simple"} in env
    assert {"name": "BP_CPYTHON_VERSION", "value": "3.12"} in env


def test_build_resources_come_from_settings_and_runtime_override():
    settings = _settings(
        build={
            "namespace": "bld",
            "registry_secret": "reg-creds",
            "resources": {"limits": {"memory": "4Gi"}},
        }
    )
    cluster = _Cluster()
    _builder(cluster, settings).build(_request())
    assert cluster.applied[-1][1]["spec"]["build"]["resources"] == {"limits": {"memory": "4Gi"}}

    # a runtime's own buildResources wins over the shared default
    runtimes = RuntimeRegistry(
        [
            RuntimeSpec(
                name="python",
                builder="python",
                buildResources={"limits": {"memory": "8Gi"}},
            )
        ]
    )
    deployer = Deployer(settings)
    deployer._clusters = {"site-a": cluster}
    cluster.applied.clear()
    KpackBuilder(settings, deployer, runtimes).build(_request())
    assert cluster.applied[-1][1]["spec"]["build"]["resources"] == {"limits": {"memory": "8Gi"}}


def test_build_rejects_a_runtime_with_no_builder():
    cluster = _Cluster()
    with pytest.raises(ValidationError, match="no `builder`"):
        _builder(cluster).build(_request(runtime="broken"))
    assert cluster.applied == [], "nothing may be applied when the runtime is unusable"


def test_build_uses_a_pinned_revision_over_the_branch():
    cluster = _Cluster()
    _builder(cluster).build(_request(revision="9f2c1ab"))
    image = cluster.applied[-1][1]
    assert image["spec"]["source"]["git"]["revision"] == "9f2c1ab"
    # the tag still follows the branch, so a rebuild replaces the same tag
    assert image["spec"]["tag"].endswith(":main")


def test_build_is_convergent_across_repeated_applies():
    cluster = _Cluster()
    builder = _builder(cluster)
    builder.build(_request())
    first = [m for _, m in cluster.applied]
    cluster.applied.clear()
    builder.build(_request())
    assert [m for _, m in cluster.applied] == first


def test_status_returns_none_when_the_site_has_no_image():
    # normal after a switchover: the caller must fall through to the KSVC status
    assert _builder(_Cluster()).status(_Cluster(), "hello", "payments") is None


def test_status_reads_the_image_from_the_build_namespace():
    from common.cluster import ResourceKind

    cluster = _Cluster(
        objects={
            (ResourceKind.KPACK_IMAGE, "fn-hello-payments"): {
                "status": {
                    "latestImage": "reg/x@sha256:1",
                    "conditions": [{"type": "Ready", "status": "True"}],
                }
            }
        }
    )
    status = _builder(cluster).status(cluster, "hello", "payments")
    assert (status.state, status.image) == ("Ready", "reg/x@sha256:1")


def test_cleanup_removes_all_three_objects_and_tolerates_absence():
    from common.cluster import ResourceKind

    objects = {
        (ResourceKind.KPACK_IMAGE, "fn-hello-payments"): {},
        (ResourceKind.SERVICE_ACCOUNT, "fn-hello-payments"): {},
        # the git Secret is already gone - cleanup must not raise
    }
    cluster = _Cluster(objects=objects)
    _builder(cluster).cleanup(cluster, "hello", "payments")
    assert [(ns, k.kind, n) for ns, k, n in cluster.deleted] == [
        ("bld", "Image", "fn-hello-payments"),
        ("bld", "ServiceAccount", "fn-hello-payments"),
    ]


def test_build_namespace_falls_back_to_workloads_when_unset():
    settings = CommonSettings(workloads_namespace="wl")
    assert settings.build_namespace == "wl"


# ------------------------------------------------------- status resolution


@pytest.mark.parametrize(
    ("overall", "state", "expected"),
    [
        # a first build: the KSVC cannot pull an image kpack has not pushed yet,
        # which must read as Building rather than Degraded
        ("Degraded", "Building", "Building"),
        ("Deploying", "Building", "Building"),
        # a failed build is the honest cause of the same symptom
        ("Degraded", "Failed", "Degraded"),
        ("Ready", "Failed", "Degraded"),
        # a finished build hands the verdict back to the KSVC rollup
        ("Ready", "Ready", "Ready"),
        ("Degraded", "Ready", "Degraded"),
    ],
)
def test_build_state_folds_into_the_overall_status(overall, state, expected):
    from api.models.common import BuildStatusView
    from api.services.workloads import _with_build_status

    assert _with_build_status(overall, BuildStatusView(state=state)) == expected


def test_no_build_leaves_the_ksvc_rollup_untouched():
    from api.services.workloads import _with_build_status

    assert _with_build_status("Ready", None) == "Ready"
    assert _with_build_status("Degraded", None) == "Degraded"


def test_building_is_a_non_terminal_poll_state():
    from api.services.deployer import status_code_for

    assert status_code_for("Building", created=False) == 202
    assert status_code_for("Building", created=True) == 202


# --------------------------------------------------- create / update paths


def _ksvc(image="reg/fn:old", branch="main"):
    from api.models.common import Scaling
    from api.services.ksvc import build_ksvc

    return build_ksvc(
        name="hello-payments",
        group="payments",
        owner="alice",
        image=image,
        offering="function",
        host="hello-payments.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
        runtime="python",
        git_url="https://git.internal/payments/hello.git",
        branch=branch,
    )


class _RecordingBuilder:
    """Counts build calls and reports a configurable build state."""

    def __init__(self, state=None):
        self.calls = 0
        self.reqs = []
        self._state = state

    def build(self, req):
        from common.contract import BuildResult

        self.calls += 1
        self.reqs.append(req)
        return BuildResult(image="reg/acme/payments/hello:main")

    def status(self, cluster, name, group):
        from common.contract import BuildStatus

        return BuildStatus(state=self._state) if self._state else None

    def cleanup(self, cluster, name, group):
        return None


async def test_config_only_update_still_reapplies_the_image_but_keeps_the_deployment():
    """Switchover self-heal: an unchanged spec must still recreate a missing Image."""
    from api.auth.claims import Principal
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from api.services.secrets import build_git_secret, git_secret_name
    from api.services.workloads import _extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster, _workload_service

    stored = build_git_secret(git_secret_name("hello-payments"), {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc()},
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    user = Principal(subject="u", username="alice", groups=["payments"])

    await FunctionService(engine).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        user,
    )

    # applied even though no build input changed - that is what recreates the
    # Image on a site that has never built this function
    assert builder.calls == 1
    assert builder.reqs[0].git_token == "ghp_stored"
    # ...but the running image is untouched: it may be a digest a finished build
    # resolved, and rewriting it back to the tag would spawn a pointless revision
    assert _extract_image(_applied_kind(cluster, "Service")[0]) == "reg/fn:old"


async def test_update_without_any_token_applies_nothing():
    from api.auth.claims import Principal
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc()})
    builder = _RecordingBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    user = Principal(subject="u", username="alice", groups=["payments"])

    await FunctionService(engine).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        user,
    )
    # no token anywhere -> the git Secret cannot be written, so no build is declared
    assert builder.calls == 0


async def test_branch_change_moves_the_deployment_to_the_new_tag():
    from api.auth.claims import Principal
    from api.models.function import FunctionUpdate
    from api.services.function import FunctionService
    from api.services.workloads import _extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster, _workload_service

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc()})
    builder = _RecordingBuilder()
    engine = _workload_service({"site-a": cluster}, builder=builder)
    user = Principal(subject="u", username="alice", groups=["payments"])

    await FunctionService(engine).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            branch="release",
            gitToken="ghp_new",
        ),
        user,
    )
    assert builder.calls == 1
    assert _extract_image(_applied_kind(cluster, "Service")[0]) == "reg/acme/payments/hello:main"


async def test_function_delete_cleans_up_the_build_objects():
    from api.auth.claims import Principal
    from api.services.function import FunctionService
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    cleaned = []

    class _Builder(_RecordingBuilder):
        def cleanup(self, cluster, name, group):
            cleaned.append((cluster.site, name, group))

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc()})
    engine = _workload_service({"site-a": cluster}, builder=_Builder())
    user = Principal(subject="u", username="alice", groups=["payments"])

    await FunctionService(engine).delete("hello", "payments", user)
    # ownerReferences cannot reach across namespaces, so an orphaned Image would
    # keep rebuilding a deleted function forever
    assert cleaned == [("site-a", "hello", "payments")]
