"""kpack build path: manifest shapes, the builder, and build-aware status."""

from __future__ import annotations

import base64

import pytest

from api.services import secrets as secret_svc
from api.services.builder import KpackBuilder
from api.services.runtimes import RuntimeRegistry, RuntimeSpec
from common import kpack
from common.config import CommonSettings, SiteConfig
from common.errors import NotFoundError, ValidationError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _settings(**over):
    base = dict(
        sites=[SiteConfig(name="site-a", cluster="a-0")],
        workloads_namespace="wl",
        build={"registry_secret": "reg-creds"},
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


def _builder(settings=None):
    return KpackBuilder(settings or _settings(), _runtimes())


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


def _plan(builder=None, **over):
    return (builder or _builder()).plan(_request(**over), {"lbl": "v"})


def _manifests(builder=None, **over):
    plan = _plan(builder, **over)
    return plan.tag, plan.replicated + plan.local


def _by_kind(manifests, kind):
    return next(m for m in manifests if m["kind"] == kind)


# --------------------------------------------------------------- manifests


def test_git_credential_host_strips_path_and_userinfo():
    host = secret_svc.git_credential_host
    assert host("https://git.internal/team/app.git") == "https://git.internal"
    assert host("https://u:p@git.internal:8443/a") == "https://git.internal:8443"
    # kpack needs a host; a bare scp-style path has none to give, so it passes through
    assert host("git@host:team/app.git") == "git@host:team/app.git"


def test_one_git_secret_serves_both_the_api_and_kpack():
    secret = secret_svc.build_git_secret(
        "hello-git", {"a": "b"}, "ghp_tok", "https://git.internal/t/a.git"
    )
    # kpack ignores an Opaque secret and one without the annotation, so both matter
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["metadata"]["annotations"][secret_svc.GIT_ANNOTATION] == "https://git.internal"
    # ...and the API must still read the token back for a later rebuild
    assert base64.b64decode(secret["data"][secret_svc.GIT_TOKEN_KEY]).decode() == "ghp_tok"


def test_service_account_carries_registry_in_both_lists():
    sa = kpack.build_service_account("fn-x", {}, "x-git", "reg-creds")
    # `secrets` is what kpack pushes with; `imagePullSecrets` is the build pod's
    assert sa["secrets"] == [{"name": "x-git"}, {"name": "reg-creds"}]
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


def test_manifests_are_pure_and_in_dependency_order():
    tag, manifests = _manifests()
    # the SA references the Secret and the Image references the SA, so a partial
    # apply must never leave a build pointing at credentials that do not exist
    assert [m["kind"] for m in manifests] == ["Secret", "ServiceAccount", "Image"]
    assert tag == "registry.internal/acme/payments/hello:main"
    # none carries a namespace: they are applied into the workload's own
    assert all("namespace" not in m["metadata"] for m in manifests)


def test_manifests_share_one_git_secret_between_the_api_and_kpack():
    _, manifests = _manifests()
    secret = _by_kind(manifests, "Secret")
    sa = _by_kind(manifests, "ServiceAccount")
    # exactly one Secret, and it is the workload's own {workload}-git
    assert len([m for m in manifests if m["kind"] == "Secret"]) == 1
    assert secret["metadata"]["name"] == "hello-payments-git"
    assert sa["secrets"][0]["name"] == "hello-payments-git"
    assert base64.b64decode(secret["data"]["password"]).decode() == "ghp_tok"


def test_image_and_service_account_share_one_name():
    _, manifests = _manifests()
    image = _by_kind(manifests, "Image")
    assert image["metadata"]["name"] == "fn-hello-payments"
    assert image["spec"]["serviceAccountName"] == "fn-hello-payments"
    assert image["spec"]["source"]["git"] == {
        "url": "https://git.internal/payments/hello.git",
        "revision": "main",
    }


def test_labels_are_stamped_on_every_manifest():
    _, manifests = _manifests()
    # the engine owner-stamps these, and GC plus the group selectors rely on labels
    assert all(m["metadata"]["labels"] == {"lbl": "v"} for m in manifests)


def test_build_env_merges_runtime_env_and_version():
    _, manifests = _manifests()
    env = _by_kind(manifests, "Image")["spec"]["build"]["env"]
    assert {"name": "PIP_INDEX_URL", "value": "https://art/simple"} in env
    assert {"name": "BP_CPYTHON_VERSION", "value": "3.12"} in env


def test_build_resources_come_from_settings():
    settings = _settings(
        build={"registry_secret": "reg-creds", "resources": {"limits": {"memory": "4Gi"}}}
    )
    _, manifests = _manifests(_builder(settings))
    # one platform-wide bound: a build is heavier than the function it produces,
    # and unset it would be BestEffort and first evicted under node pressure
    assert _by_kind(manifests, "Image")["spec"]["build"]["resources"] == {
        "limits": {"memory": "4Gi"}
    }


def test_manifests_reject_a_runtime_with_no_builder():
    with pytest.raises(ValidationError, match="no `builder`"):
        _manifests(runtime="broken")


def test_manifests_use_a_pinned_revision_over_the_branch():
    _, manifests = _manifests(revision="9f2c1ab")
    image = _by_kind(manifests, "Image")
    assert image["spec"]["source"]["git"]["revision"] == "9f2c1ab"
    # the tag still follows the branch, so a rebuild replaces the same tag
    assert image["spec"]["tag"].endswith(":main")


def test_manifests_are_convergent_across_repeated_calls():
    builder = _builder()
    assert _manifests(builder)[1] == _manifests(builder)[1]


def test_the_git_credential_replicates_but_the_image_does_not():
    plan = _plan()
    # every site must be able to rebuild after a switchover, and a token is not
    # recoverable if its only copy was on the site that went away
    assert [m["kind"] for m in plan.replicated] == ["Secret"]
    # ...but only one site builds, or both race to push the same tag
    assert [m["kind"] for m in plan.local] == ["ServiceAccount", "Image"]


def test_pull_secret_is_the_credential_kpack_pushed_with():
    # one image, one registry, one credential - a function never supplies its own
    assert _builder().pull_secret == "reg-creds"


# ------------------------------------------------------------------ status


class _StatusCluster:
    def __init__(self, objects=None):
        self.site = self.name = "site-a"
        self._objects = objects or {}

    def get(self, kind, name=None, label_selector=None):
        try:
            return self._objects[(kind, name)]
        except KeyError:
            raise NotFoundError(f"{name} not found") from None


def test_status_returns_none_when_the_site_has_no_image():
    # normal after a switchover: the caller must fall through to the KSVC status
    assert _builder().status(_StatusCluster(), "hello", "payments") is None


def test_status_reads_the_image_by_its_derived_name():
    from common.cluster import ResourceKind

    cluster = _StatusCluster(
        {
            (ResourceKind.KPACK_IMAGE, "fn-hello-payments"): {
                "status": {
                    "latestImage": "reg/x@sha256:1",
                    "conditions": [{"type": "Ready", "status": "True"}],
                }
            }
        }
    )
    status = _builder().status(cluster, "hello", "payments")
    assert (status.state, status.image) == ("Ready", "reg/x@sha256:1")


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
    """Counts manifest requests and reports a configurable build state."""

    pull_secret = "reg-creds"

    def __init__(self, state=None):
        self.calls = 0
        self.reqs = []
        self._state = state

    def image_ref(self, req):
        return "reg/acme/payments/hello:main"

    def plan(self, req, labels):
        from common.contract import BuildPlan

        self.calls += 1
        self.reqs.append(req)
        return BuildPlan(
            tag=self.image_ref(req),
            replicated=[
                secret_svc.build_git_secret(
                    "hello-payments-git", labels, req.git_token, req.git_url
                )
            ],
            local=[
                {
                    "apiVersion": "kpack.io/v1alpha2",
                    "kind": "Image",
                    "metadata": {"name": "fn-hello-payments", "labels": dict(labels)},
                    "spec": {},
                }
            ],
        )

    def status(self, cluster, name, group):
        from common.contract import BuildStatus

        return BuildStatus(state=self._state) if self._state else None


class _SiteAwareBuilder(_RecordingBuilder):
    """Reports a build only on the site that holds the Image, as a cluster does."""

    def __init__(self, built_on: str, state: str):
        super().__init__(state)
        self._built_on = built_on

    def status(self, cluster, name, group):
        from common.contract import BuildStatus

        return BuildStatus(state=self._state) if cluster.site == self._built_on else None


def _principal():
    from api.auth.claims import Principal

    return Principal(subject="u", username="alice", groups=["payments"])


def _create_spec():
    from api.models.function import FunctionCreate

    return FunctionCreate(
        name="hello",
        gitRepo="https://git.internal/payments/hello.git",
        gitToken="ghp_tok",
        runtime="python",
    )


def _function_service(clusters, builder, local_site=None):
    from api.services.function import FunctionService
    from tests.conftest import runtime_registry
    from tests.test_auth_and_deployer import _workload_service

    return FunctionService(
        _workload_service(clusters, builder=builder, local_site=local_site), runtime_registry()
    )


async def test_create_deploys_with_the_platform_pull_secret():
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    cluster = _ApplyCluster("site-a", {})
    svc = _function_service({"site-a": cluster}, _RecordingBuilder())
    await svc.create("payments", _create_spec(), _principal())

    pod = _applied_kind(cluster, "Service")[0]["spec"]["template"]["spec"]
    # the built image lives on the platform registry, so it pulls with the same
    # credential kpack pushed it with - the caller supplies no registry details
    assert pod["imagePullSecrets"] == [{"name": "reg-creds"}]


async def test_build_manifests_are_applied_and_owned_by_the_ksvc():
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    cluster = _ApplyCluster("site-a", {})
    svc = _function_service({"site-a": cluster}, _RecordingBuilder())
    await svc.create("payments", _create_spec(), _principal())

    images = _applied_kind(cluster, "Image")
    assert len(images) == 1
    # the ownerReference is what deletes the Image with the function - without it
    # an orphan keeps rebuilding a function that no longer exists
    owners = images[0]["metadata"]["ownerReferences"]
    assert [(o["kind"], o["name"]) for o in owners] == [("Service", "hello-payments")]


def _git_secrets(cluster):
    from tests.test_auth_and_deployer import _applied_kind

    return [s for s in _applied_kind(cluster, "Secret") if s["metadata"]["name"].endswith("-git")]


async def test_only_one_site_builds_but_every_site_gets_the_credential():
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    local = _ApplyCluster("site-a", {})
    remote = _ApplyCluster("site-b", {})
    svc = _function_service(
        {"site-a": local, "site-b": remote}, _RecordingBuilder(), local_site="site-a"
    )
    await svc.create("payments", _create_spec(), _principal())

    # one builder: fanning the Image out would have both sites build the same
    # source and race to push the same tag (§9.1)
    assert len(_applied_kind(local, "Image")) == 1
    assert _applied_kind(remote, "Image") == []
    assert len(_applied_kind(remote, "Service")) == 1  # ...but the KSVC goes everywhere
    # the token, though, must be everywhere: after a switchover the new local
    # site rebuilds from the copy it already holds, and nothing can recover a
    # token whose only copy was on the site that went away (§9.5)
    assert len(_git_secrets(local)) == 1
    assert len(_git_secrets(remote)) == 1


async def test_a_site_set_excluding_the_local_one_still_builds_somewhere():
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    local = _ApplyCluster("site-a", {})
    remote = _ApplyCluster("site-b", {})
    svc = _function_service(
        {"site-a": local, "site-b": remote}, _RecordingBuilder(), local_site="site-a"
    )
    spec = _create_spec()
    spec.sites = ["site-b"]  # the local site is not a target
    await svc.create("payments", spec, _principal())

    # the build falls back to a targeted site; skipping it would leave the KSVC
    # pointing at a tag that nothing ever builds
    assert _applied_kind(local, "Service") == []
    assert len(_applied_kind(remote, "Image")) == 1


def test_build_status_is_read_from_the_site_that_built_not_always_the_local_one():
    """The read has to look where the write put the Image.

    apply_workload builds on the local site only when it is a target, and falls
    back to the first target otherwise. A read fixed on the local site therefore
    finds no Image for such a function and folds in no build state at all - so a
    normal first build reports Degraded, the exact reading _with_build_status
    exists to prevent.
    """
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    local = _ApplyCluster("site-a", {})
    remote = _ApplyCluster("site-b", {})
    svc = _workload_service(
        {"site-a": local, "site-b": remote},
        builder=_SiteAwareBuilder(built_on="site-b", state="Building"),
        local_site="site-a",
    )

    view = svc._build_status("hello", "payments")
    assert view is not None, "no build state found: the read only looked at the local site"
    assert view.state == "Building"


def test_reading_the_build_status_does_not_fan_out_when_the_local_site_has_it():
    """The fallback must not cost every GET an extra cross-site call."""
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    seen = []

    class _Counting(_RecordingBuilder):
        def status(self, cluster, name, group):
            from common.contract import BuildStatus

            seen.append(cluster.site)
            return BuildStatus(state="Ready")

    svc = _workload_service(
        {"site-a": _ApplyCluster("site-a", {}), "site-b": _ApplyCluster("site-b", {})},
        builder=_Counting(),
        local_site="site-a",
    )

    assert svc._build_status("hello", "payments").state == "Ready"
    assert seen == ["site-a"]


async def test_config_only_update_reapplies_the_build_but_keeps_the_deployment():
    """Switchover self-heal: an unchanged spec must still recreate a missing Image."""
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.workloads import _extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a", {"hello-payments": _ksvc()}, secrets={"hello-payments-git": stored}
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        _principal(),
    )
    # emitted even though no build input changed - that is what recreates the
    # Image on a site that has never built this function
    assert builder.calls == 1
    assert builder.reqs[0].git_token == "ghp_stored"
    assert len(_applied_kind(cluster, "Image")) == 1
    # ...but the running image is untouched: it may be a digest a finished build
    # resolved, and rewriting it back to the tag would spawn a pointless revision
    assert _extract_image(_applied_kind(cluster, "Service")[0]) == "reg/fn:old"


async def test_update_without_any_token_emits_no_build():
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc()})
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        _principal(),
    )
    # no token anywhere -> the git Secret cannot be written, so no build is declared
    assert builder.calls == 0
    assert _applied_kind(cluster, "Image") == []


async def test_branch_change_moves_the_deployment_to_the_new_tag():
    from api.models.function import FunctionUpdate
    from api.services.workloads import _extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc()})
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            branch="release",
            gitToken="ghp_new",
        ),
        _principal(),
    )
    assert builder.calls == 1
    assert _extract_image(_applied_kind(cluster, "Service")[0]) == "reg/acme/payments/hello:main"


# ------------------------------------------------------- request validation


def test_build_request_validates_its_own_name_and_group():
    """The build path is reachable off the HTTP edge, so it cannot trust inputs."""
    import pydantic

    for bad in ({"name": "Bad Name!"}, {"group": "has space"}, {"name": "x" * 64}):
        with pytest.raises(pydantic.ValidationError):
            _request(**bad)


def test_build_request_normalizes_the_group_the_same_way_the_http_edge_does():
    """A build is addressed by group, so it must land on the HTTP edge's spelling.

    Uppercase and "_" are legal in an SSO group and normalized (not rejected) at
    the edge. If the contract validated without normalizing, the same team would
    build to a different repository path than it deploys to.
    """
    assert _request(group="My_Team").group == "my-team"
    assert _request(group="GGD-1234-Platforms").group == "platforms"


@pytest.mark.parametrize("bad", ["", " ", "has space", "-leading", "a..b", "x/", "with:colon"])
def test_build_request_rejects_unusable_branches(bad):
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _request(branch=bad)


def test_a_slashed_branch_builds_that_ref_but_pushes_a_legal_tag():
    """`feature/login` is an everyday branch; `/` is illegal in an OCI tag."""
    plan = _plan(branch="feature/login")
    assert plan.tag == "registry.internal/acme/payments/hello:feature-login"
    # the git revision keeps the real ref - only the tag is a projection
    image = _by_kind(plan.local, "Image")
    assert image["spec"]["source"]["git"]["revision"] == "feature/login"
    assert image["spec"]["tag"] == plan.tag


def test_image_tag_projection_rules():
    from common.names import image_tag

    assert image_tag("main") == "main"
    assert image_tag("release/1.2.x") == "release-1.2.x"
    # a tag must start alphanumeric or '_', so leading '.'/'-' are dropped
    assert image_tag(".hidden") == "hidden"
    assert len(image_tag("x" * 200)) == 128


def test_a_branch_with_no_ascii_still_projects_to_a_usable_tag():
    """Git refs are UTF-8, so every character can be one the tag grammar forbids.

    Such a branch replaces to all '-' and strips to "", and an empty tag makes
    the reference "repo:" - malformed on both the Image and the KSVC. The
    fallback must be non-empty, stable (active/active applies converge) and
    distinct per branch.
    """
    import re

    from common.contract import image_reference
    from common.names import image_tag

    for branch in ("功能", "релиз", "機能/ログイン"):
        tag = image_tag(branch)
        assert tag, f"{branch!r} projected to an empty tag"
        assert re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$", tag)

    assert image_tag("功能") == image_tag("功能")
    assert image_tag("功能") != image_tag("релиз")
    # a branch only partly non-ASCII keeps its readable part, no fallback needed
    assert image_tag("功能-login") == "login"

    req = _request(branch="功能")
    assert not image_reference("reg.internal", req).endswith(":")


# ------------------------------------------------------ the runtimes contract


def test_runtime_spec_declares_what_the_builder_reads():
    """These are the ConfigMap contract, not incidental extra keys."""
    declared = set(RuntimeSpec.model_fields)
    assert {
        "name",
        "builder",
        "versionEnv",
        "defaultVersion",
        "versions",
        "buildEnv",
    } <= declared


def test_runtime_spec_keeps_keys_it_does_not_know():
    # a newer chart must be deployable ahead of the API
    spec = RuntimeSpec(name="python", somethingNew="x")
    assert spec.model_dump()["somethingNew"] == "x"


def test_unquoted_yaml_numbers_do_not_break_the_runtimes_file():
    # `defaultVersion: 3.12` unquoted is a float in YAML; rejecting it would take
    # the whole runtimes file down over a missing pair of quotes
    spec = RuntimeSpec(name="python", defaultVersion=3.12, versions=[3.11, 3.12])
    assert spec.defaultVersion == "3.12"
    assert spec.versions == ["3.11", "3.12"]


def test_an_explicit_version_in_build_env_is_not_overridden():
    runtimes = RuntimeRegistry(
        [
            RuntimeSpec(
                name="python",
                builder="python",
                versionEnv="BP_CPYTHON_VERSION",
                defaultVersion="3.12",
                buildEnv=[{"name": "BP_CPYTHON_VERSION", "value": "3.11"}],
            )
        ]
    )
    plan = KpackBuilder(_settings(), runtimes).plan(_request(), {})
    env = _by_kind(plan.local, "Image")["spec"]["build"]["env"]
    versions = [e["value"] for e in env if e["name"] == "BP_CPYTHON_VERSION"]
    assert versions == ["3.11"], "a deliberate buildEnv entry must win over the default"
