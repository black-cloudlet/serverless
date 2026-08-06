"""kpack build path: manifest shapes, the builder, and build-aware status."""

from __future__ import annotations

import base64

import pytest

from api.services.builder.kpack_backend import KpackBackend
from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec
from api.services.manifests import secrets as secret_svc
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
    return KpackBackend(settings or _settings(), _runtimes())


def _request(**over):
    from common.build import BuildRequest

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


def test_build_cache_defaults_to_the_registry_not_a_volume():
    _, manifests = _manifests()
    # the volume form is a PVC per Image, so it scales with the function count
    assert _by_kind(manifests, "Image")["spec"]["cache"] == {
        "registry": {"tag": "registry.internal/acme/payments/hello_cache:latest"}
    }


def test_cache_repository_can_never_collide_with_a_function_image():
    tag, manifests = _manifests(branch="cache")
    cache = _by_kind(manifests, "Image")["spec"]["cache"]["registry"]["tag"]
    # a branch named "cache" projects to the tag "cache", so a reserved tag in
    # the function's own repository would collide; the extra segment rules it out
    assert tag == "registry.internal/acme/payments/hello:cache"
    assert cache.rsplit(":", 1)[0] != tag.rsplit(":", 1)[0]


def test_cache_repository_does_not_move_with_the_branch():
    builder = _builder()
    # one Image per function, so one cache: keying it by branch would start cold
    # on every branch change
    assert builder.cache_ref(_request(branch="main")) == builder.cache_ref(
        _request(branch="feature/login")
    )


def test_cache_can_be_left_to_the_kpack_install():
    settings = _settings(build={"registry_secret": "reg-creds", "cache": "inherit"})
    _, manifests = _manifests(_builder(settings))
    # "write nothing" is not "no cache" - it hands the choice back to kpack
    assert "cache" not in _by_kind(manifests, "Image")["spec"]


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


def test_the_built_image_never_reaches_a_function_response():
    """A function's client deals in source, not images.

    The digest stays on the internal BuildStatus, which the build service reads
    to move the KSVC; the view carries state and reason only.
    """
    from api.models.common import BuildStatusView
    from api.models.function import FunctionResponse
    from common.build import BuildStatus

    assert "image" in BuildStatus.__dataclass_fields__
    assert "image" not in BuildStatusView.model_fields

    body = FunctionResponse(
        name="hello",
        group="payments",
        type="function",
        hostname="hello-payments.ex.com",
        overallStatus="Building",
        build=BuildStatusView(state="Building"),
    ).model_dump()
    assert "image" not in body
    assert "image" not in body["build"]


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
    from api.services.state.ksvc_state import with_build_status

    assert with_build_status(overall, BuildStatusView(state=state)) == expected


def test_no_build_leaves_the_ksvc_rollup_untouched():
    from api.services.state.ksvc_state import with_build_status

    assert with_build_status("Ready", None) == "Ready"
    assert with_build_status("Degraded", None) == "Degraded"


def test_a_running_build_folds_into_the_per_site_rows_too():
    """A failing site under a running build reports the build, not the pull error.

    Without this the detail view contradicts itself: `Building` in the header and
    `Failed` - `Unable to fetch image ...` in the sites table right below it.
    """
    from api.models.common import BuildStatusView, SiteStatus
    from api.services.state.ksvc_state import sites_with_build_status

    sites = [
        SiteStatus(
            site="a",
            status="Failed",
            revision="fn-team-00001",
            error='Unable to fetch image "reg/team/fn:main": not found',
        ),
        SiteStatus(site="b", status="Ready", revision="fn-team-00001"),
    ]
    folded = sites_with_build_status(sites, BuildStatusView(state="Building"))

    assert folded[0].status == "Building"
    assert folded[0].error is None
    assert folded[0].revision == "fn-team-00001"  # everything else is untouched
    assert folded[1].status == "Ready"  # a site that isn't failing is left alone


@pytest.mark.parametrize("state", ["Failed", "Ready", "Unknown"])
def test_only_a_running_build_masks_a_failing_site(state):
    """A build that is not in flight leaves the rows exactly as the KSVC read them.

    A failed build is the case that matters: then the image genuinely never
    arrives, so the site's pull error is the truth and must stay visible.
    """
    from api.models.common import BuildStatusView, SiteStatus
    from api.services.state.ksvc_state import sites_with_build_status

    sites = [SiteStatus(site="a", status="Failed", error="boom")]

    assert sites_with_build_status(sites, BuildStatusView(state=state)) == sites
    assert sites_with_build_status(sites, None) == sites


def test_building_is_a_non_terminal_poll_state():
    from api.services.sites.deployer import status_code_for

    assert status_code_for("Building", created=False) == 202
    assert status_code_for("Building", created=True) == 202


# --------------------------------------------------- create / update paths


# What a finished build left on the KSVC: a digest, in the repository
# _RecordingBuilder pushes to. A fake deployed somewhere else would read as a
# moved registry layout, which is a different test (see Registry layout).
DEPLOYED = "reg/acme/payments/hello@sha256:" + "c" * 64


def _ksvc(image=DEPLOYED, branch="main", path="", version=None, port=None):
    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc

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
        path=path,
        version=version,
        port=port,
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
        from common.build import BuildPlan

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
        from common.build import BuildStatus

        return BuildStatus(state=self._state) if self._state else None


class _SiteAwareBuilder(_RecordingBuilder):
    """Reports a build only on the site that holds the Image, as a cluster does."""

    def __init__(self, built_on: str, state: str):
        super().__init__(state)
        self._built_on = built_on

    def status(self, cluster, name, group):
        from common.build import BuildStatus

        return BuildStatus(state=self._state) if cluster.site == self._built_on else None


def _principal():
    from api.auth.claims import Principal

    return Principal(subject="u", username="alice", groups=["payments"])


def _create_spec(**over):
    from api.models.function import FunctionCreate

    base = dict(
        name="hello",
        gitRepo="https://git.internal/payments/hello.git",
        gitToken="ghp_tok",
        runtime="python",
    )
    base.update(over)
    return FunctionCreate(**base)


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
    # source and race to push the same tag (docs/BUILDING.md - Active/Active)
    assert len(_applied_kind(local, "Image")) == 1
    assert _applied_kind(remote, "Image") == []
    assert len(_applied_kind(remote, "Service")) == 1  # ...but the KSVC goes everywhere
    # the token, though, must be everywhere: nothing can recover a token whose
    # only copy was on the site that went away (docs/BUILDING.md - Active/Active)
    assert len(_git_secrets(local)) == 1
    assert len(_git_secrets(remote)) == 1


async def test_the_local_site_builds_even_when_it_runs_no_copy_of_the_function():
    """The build site is the local one, always - deployment targets do not move it.

    A function targeted elsewhere still builds here and pushes to the shared
    registry, which the running site pulls from. The local site gets the build
    objects and nothing else: no KSVC, no DomainMapping.
    """
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    local = _ApplyCluster("site-a", {})
    remote = _ApplyCluster("site-b", {})
    svc = _function_service(
        {"site-a": local, "site-b": remote}, _RecordingBuilder(), local_site="site-a"
    )
    spec = _create_spec()
    spec.sites = ["site-b"]  # the local site is not a target
    await svc.create("payments", spec, _principal())

    assert len(_applied_kind(local, "Image")) == 1
    assert _applied_kind(remote, "Image") == []
    # ...and only the build: the workload itself runs where it was asked to
    assert _applied_kind(local, "Service") == []
    assert _applied_kind(local, "DomainMapping") == []
    assert len(_applied_kind(remote, "Service")) == 1
    # the builder needs the token on the site that clones
    assert len(_git_secrets(local)) == 1


async def test_build_objects_on_a_non_target_local_site_are_deleted_explicitly():
    """Nothing owns them there, so nothing cascades when the function goes.

    An ownerReference must name an owner in the same cluster, and the KSVC that
    would be it was never applied here. A leftover Image would keep rebuilding a
    function that no longer exists.
    """
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    local = _ApplyCluster("site-a", {})
    remote = _ApplyCluster("site-b", {})
    svc = _function_service(
        {"site-a": local, "site-b": remote}, _RecordingBuilder(), local_site="site-a"
    )
    spec = _create_spec()
    spec.sites = ["site-b"]
    await svc.create("payments", spec, _principal())
    assert len(_applied_kind(local, "Image")) == 1

    await svc.delete("hello", "payments", _principal())

    deleted = {(k.value[1], n) for k, n in local.deleted}
    assert ("Image", "fn-hello-payments") in deleted
    assert ("ServiceAccount", "fn-hello-payments") in deleted
    assert ("Secret", "hello-payments-git") in deleted


def test_reading_the_build_status_does_not_fan_out_when_the_local_site_has_it():
    """The fallback must not cost every GET an extra cross-site call."""
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    seen = []

    class _Counting(_RecordingBuilder):
        def status(self, cluster, name, group):
            from common.build import BuildStatus

            seen.append(cluster.site)
            return BuildStatus(state="Ready")

    svc = _workload_service(
        {"site-a": _ApplyCluster("site-a", {}), "site-b": _ApplyCluster("site-b", {})},
        builder=_Counting(),
        local_site="site-a",
    )

    from api.services.offering import FUNCTION

    status = FUNCTION.build_status(svc.builder, svc.deployer.local_cluster(), "hello", "payments")
    assert status.state == "Ready"
    assert seen == ["site-a"]


async def test_config_only_update_reapplies_the_build_but_keeps_the_deployment():
    """Switchover self-heal: an unchanged spec must still recreate a missing Image."""
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
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
    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


async def test_changing_the_source_path_rebuilds_but_leaves_the_running_image():
    """path is a build input: a different directory is a different application."""
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc(path="services/api")},
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            path="services/worker",
        ),
        _principal(),
    )

    assert builder.reqs[0].path == "services/worker"
    # The build is re-declared, but the workload keeps the digest it is serving:
    # the tag still resolves to that same digest until the build lands, so
    # writing it would cut a revision of identical code. The controller supplies
    # the new digest (docs/BUILDING.md - Digest propagation).
    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


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


async def test_a_branch_change_rebuilds_without_disturbing_the_running_image():
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
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
    # A branch change re-tags where the build pushes, but the running digest is
    # untouched until that build finishes and the controller rolls it out.
    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


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

    from common.build import image_reference
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


def test_source_path_selects_a_directory_without_changing_the_ref():
    """subPath picks the build directory; the whole repo is still cloned."""
    _, manifests = _manifests(path="services/api")
    source = _by_kind(manifests, "Image")["spec"]["source"]

    assert source["subPath"] == "services/api"
    assert source["git"] == {
        "url": "https://git.internal/payments/hello.git",
        "revision": "main",
    }


def test_no_source_path_leaves_sub_path_off_the_image():
    """A monorepo field must not change the manifest of a root-built function."""
    _, manifests = _manifests()
    assert "subPath" not in _by_kind(manifests, "Image")["spec"]["source"]


def test_the_source_path_does_not_change_the_image_tag():
    """Two directories in one repo are two functions, told apart by name."""
    assert _plan(path="services/api").tag == _plan().tag


@pytest.mark.parametrize(
    ("given", "expected"),
    [("src", "src"), ("/src", "src"), ("src/", "src"), ("  a/b  ", "a/b"), ("", "")],
)
def test_source_path_normalization(given, expected):
    from common.names import validate_source_path

    assert validate_source_path(given) == expected


@pytest.mark.parametrize("bad", ["..", "../etc", "a/../b", "a//b", "a/./b", "a b", "a\\b"])
def test_source_path_rejects_escapes_and_unusable_segments(bad):
    """kpack resolves subPath inside the clone; '..' would build something else."""
    from common.names import validate_source_path

    with pytest.raises(ValueError):
        validate_source_path(bad)


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
    plan = KpackBackend(_settings(), runtimes).plan(_request(), {})
    env = _by_kind(plan.local, "Image")["spec"]["build"]["env"]
    versions = [e["value"] for e in env if e["name"] == "BP_CPYTHON_VERSION"]
    assert versions == ["3.11"], "a deliberate buildEnv entry must win over the default"


def _version_runtimes(**over):
    kwargs = dict(
        name="go",
        builder="go",
        versionEnv="BP_GO_VERSION",
        defaultVersion="1.24",
        versions=["1.23", "1.24", "1.25"],
    )
    kwargs.update(over)
    return RuntimeRegistry([RuntimeSpec(**kwargs)])


def _version_env(runtimes, **req):
    plan = KpackBackend(_settings(), runtimes).plan(_request(runtime="go", **req), {})
    env = _by_kind(plan.local, "Image")["spec"]["build"]["env"]
    return [e["value"] for e in env if e["name"] == "BP_GO_VERSION"]


def test_the_version_env_is_always_written_even_when_the_caller_omits_one():
    """An omitted version must still pin OUR default, never fall through.

    Leaving BP_*_VERSION unset hands the choice to the buildpack's own default,
    which moves when the buildpackage is upgraded - so an untouched function
    could silently rebuild on a different language version. Airgapped it is
    worse: only the advertised versions are mirrored, so the buildpack's default
    may have no toolchain to download at all.
    """
    assert _version_env(_version_runtimes()) == ["1.24"]


def test_a_requested_version_is_what_gets_built():
    assert _version_env(_version_runtimes(), version="1.25") == ["1.25"]


def test_a_caller_version_overrides_an_operator_build_env_pin():
    """The pin is the default, not a veto.

    An operator who wants no choice at all leaves `versions` empty, and the
    request is rejected before it reaches here (see FunctionService).
    """
    pinned = _version_runtimes(buildEnv=[{"name": "BP_GO_VERSION", "value": "1.23"}])
    assert _version_env(pinned) == ["1.23"], "omitted -> the operator's pin"
    assert _version_env(pinned, version="1.25") == ["1.25"], "asked for -> the caller's"


def test_exactly_one_version_entry_is_emitted():
    """Two entries for the same name would leave the build ambiguous."""
    pinned = _version_runtimes(buildEnv=[{"name": "BP_GO_VERSION", "value": "1.23"}])
    plan = KpackBackend(_settings(), pinned).plan(_request(runtime="go", version="1.25"), {})
    env = _by_kind(plan.local, "Image")["spec"]["build"]["env"]
    assert [e["name"] for e in env].count("BP_GO_VERSION") == 1


def test_a_runtime_naming_no_version_env_gets_none_invented():
    runtimes = RuntimeRegistry([RuntimeSpec(name="go", builder="go")])
    plan = KpackBackend(_settings(), runtimes).plan(_request(runtime="go"), {})
    image = _by_kind(plan.local, "Image")
    env = (image["spec"].get("build") or {}).get("env") or []
    assert not [e for e in env if e["name"].startswith("BP_")]


async def test_changing_the_version_rebuilds_but_leaves_the_running_image():
    """The language version is a build input like branch or path."""
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc(version="3.11")},
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            version="3.12",
        ),
        _principal(),
    )

    assert builder.reqs[0].version == "3.12"
    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


async def test_omitting_the_version_returns_to_the_default_and_rebuilds():
    """`version` is replaced, not kept - like branch and runtime, unlike gitToken.

    So a PUT that drops it is a deliberate "give me the platform default", and
    that is a different build from the pinned one it replaces.
    """
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc(version="3.11")},
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(gitRepo="https://git.internal/payments/hello.git", runtime="python"),
        _principal(),
    )

    assert builder.reqs[0].version is None  # -> the builder pins defaultVersion
    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


async def test_resending_the_same_version_is_not_a_rebuild():
    """A config-only edit that echoes the stored version must not disturb it."""
    from api.models.common import Scaling
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc(version="3.11")},
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            version="3.11",
            scaling=Scaling(minScale=2, maxScale=2),
        ),
        _principal(),
    )

    assert extract_image(_applied_kind(cluster, "Service")[0]) == DEPLOYED


# ------------------------------------------- the BuildBackend protocol itself


def test_the_kpack_backend_matches_the_build_backend_protocol():
    """Every ``BuildBackend`` member exists on ``KpackBackend`` with the same signature.

    The protocol's whole job is stopping the API and a future build service from
    drifting apart, and nothing else enforces it: there is no type checker in the
    dev extra, so a protocol nobody executes is a comment. This is the check -
    it caught ``pull_secret`` being declared ``-> str`` while the implementation
    returned ``str | None``.

    Both modules use ``from __future__ import annotations``, so the annotations
    compare as the source strings: a difference in spelling is a difference here,
    which is the point.
    """
    import inspect

    from common.build import BuildBackend

    for name, declared in vars(BuildBackend).items():
        if name.startswith("_") or not callable(getattr(declared, "fget", declared)):
            continue
        implemented = getattr(KpackBackend, name, None)
        assert implemented is not None, f"KpackBackend is missing BuildBackend.{name}"
        # A property must stay a property: callers read `backend.pull_secret`.
        assert isinstance(declared, property) == isinstance(implemented, property), (
            f"BuildBackend.{name} and KpackBackend.{name} disagree on being a property"
        )
        declared_fn = declared.fget if isinstance(declared, property) else declared
        implemented_fn = implemented.fget if isinstance(implemented, property) else implemented
        assert inspect.signature(declared_fn) == inspect.signature(implemented_fn), (
            f"KpackBackend.{name}{inspect.signature(implemented_fn)} does not match "
            f"BuildBackend.{name}{inspect.signature(declared_fn)}"
        )


# --------------------------------------------------- a function's port


def _pod_ports(cluster):
    from tests.test_auth_and_deployer import _applied_kind

    container = _applied_kind(cluster, "Service")[0]["spec"]["template"]["spec"]["containers"][0]
    return container.get("ports")


async def test_a_function_omitting_a_port_is_stamped_with_the_default():
    """The normal case: 8080, written explicitly rather than left to convention.

    Stamping it is the point - it is the same port Knative would have injected,
    but now it is a value in the manifest that a read can report, instead of a
    default a client has to already know.
    """
    from tests.test_auth_and_deployer import _ApplyCluster

    cluster = _ApplyCluster("site-a", {})
    svc = _function_service({"site-a": cluster}, _RecordingBuilder())
    body, _ = await svc.create("payments", _create_spec(), _principal())

    assert _pod_ports(cluster) == [{"containerPort": 8080}]
    assert body.port == 8080


async def test_a_function_can_pin_a_port_for_an_app_that_hardcodes_one():
    from tests.test_auth_and_deployer import _ApplyCluster

    cluster = _ApplyCluster("site-a", {})
    svc = _function_service({"site-a": cluster}, _RecordingBuilder())
    body, _ = await svc.create("payments", _create_spec(port=9000), _principal())

    assert _pod_ports(cluster) == [{"containerPort": 9000}]
    assert body.port == 9000  # echoed back, like a container's


async def test_a_function_port_is_replaced_on_update_not_kept():
    """Omitting it returns the function to 8080, as omitting `version` does.

    The rule is the container's - the offerings share one port contract: only
    secret material is keep-on-omit, because only secret material cannot be read
    back. A port can (GET reports it), so a PUT that leaves it out is asking for
    the default, not for no change.
    """
    from api.models.function import FunctionUpdate
    from tests.test_auth_and_deployer import _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc(port=9000)},
        secrets={"hello-payments-git": stored},
    )
    await _function_service({"site-a": cluster}, _RecordingBuilder()).update(
        "payments",
        "hello",
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
        ),
        _principal(),
    )

    assert _pod_ports(cluster) == [{"containerPort": 8080}]  # back to the default, not 9000


async def test_a_function_port_is_reported_on_read():
    from api.services.offering import FUNCTION
    from tests.test_auth_and_deployer import _ApplyCluster, _workload_service

    cluster = _ApplyCluster("site-a", {"hello-payments": _ksvc(port=9000)})
    engine = _workload_service({"site-a": cluster}, builder=_RecordingBuilder())
    body = await engine.get(FUNCTION, "hello", _principal(), "payments")

    assert body.port == 9000


# ----------------------------------------------- the Offering protocol itself


def test_both_offerings_match_the_offering_protocol():
    """Same guard as the BuildBackend conformance check, for the same reason.

    The engine no longer branches on which offering it has; it calls these
    members. A missing or renamed one would be an AttributeError on a live
    request path, and with no type checker in the dev extra nothing else looks.
    """
    import inspect

    from api.services.offering import CONTAINER, FUNCTION, Offering

    for impl in (FUNCTION, CONTAINER):
        for name, declared in vars(Offering).items():
            if name.startswith("_"):
                continue
            assert hasattr(impl, name), f"{type(impl).__name__} is missing Offering.{name}"
            if isinstance(declared, property):
                continue  # a declared property may be satisfied by a class attribute
            # `self` is bound on the implementation's method but declared on the
            # protocol's, so drop it before the signatures can be compared.
            declared_sig = inspect.signature(declared)
            unbound = declared_sig.replace(parameters=list(declared_sig.parameters.values())[1:])
            assert inspect.signature(getattr(impl, name)) == unbound, (
                f"{type(impl).__name__}.{name} does not match Offering.{name}"
            )


async def test_changing_only_the_port_does_not_rebuild():
    """The port is a runtime field, not a build input - it never reaches kpack.

    `BuildRequest` has no port, so nothing about the image depends on it: a
    buildpack image serves whatever `$PORT` Knative injects, and the port is
    decided when the KSVC is applied, not when the image is compiled. So a
    port-only edit must behave like any other config-only edit - keep the
    running image, spawn one new revision - rather than making the caller wait
    out a build to move a port.
    """
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a",
        {"hello-payments": _ksvc()},  # deployed at the digest a build resolved
        secrets={"hello-payments-git": stored},
    )
    builder = _RecordingBuilder()
    await _function_service({"site-a": cluster}, builder).update(
        "payments",
        "hello",
        # every build input identical to what is stored; only the port moves
        FunctionUpdate(
            gitRepo="https://git.internal/payments/hello.git",
            runtime="python",
            port=9000,
        ),
        _principal(),
    )

    ksvc = _applied_kind(cluster, "Service")[0]
    assert extract_image(ksvc) == DEPLOYED  # NOT moved to the build tag
    assert _pod_ports(cluster) == [{"containerPort": 9000}]


# -------------------------------------------------------------------- build


def _build_obj(number, image="fn-hello-payments"):
    return {
        "apiVersion": "kpack.io/v1alpha2",
        "kind": "Build",
        "metadata": {
            "name": f"{image}-build-{number}",
            "labels": {
                kpack.IMAGE_LABEL: image,
                kpack.BUILD_NUMBER_LABEL: str(number),
            },
        },
    }


def test_latest_build_orders_on_the_number_not_the_string():
    # "10" sorts before "9" as text, and the annotation only counts on the Build
    # kpack actually looks at - so a text order would trigger nothing at all
    builds = [_build_obj(9), _build_obj(10), _build_obj(2)]

    assert kpack.latest_build(builds)["metadata"]["name"] == "fn-hello-payments-build-10"
    assert kpack.latest_build([]) is None
    # an object that is not one of kpack's numbered Builds gives no ordering key
    assert kpack.latest_build([{"metadata": {"labels": {}}}]) is None


class _BuildCluster:
    """Serves a function's kpack Builds and records what was patched onto them."""

    def __init__(self, builds, site="site-a"):
        self.site = site
        self.name = site
        self._builds = list(builds)
        self.patched = []

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from common.cluster import ResourceKind

        assert kind == ResourceKind.KPACK_BUILD
        assert label_selector == f"{kpack.IMAGE_LABEL}=fn-hello-payments"
        return list(self._builds)

    def patch(self, kind, name, body):
        self.patched.append((kind, name, body))
        return {}


def test_the_trigger_annotates_the_latest_build_and_leaves_the_image_alone():
    """The Image spec stays a pure function of the function definition.

    kpack asks whether the *last Build* carries the annotation, so that is where
    it goes. A nonce on the Image would look like a change on every apply and
    rebuild forever under active/active, and dropping it again on the next
    ordinary PUT would rebuild once more (docs/BUILDING.md - Convergence rules).
    """
    from common.cluster import ResourceKind

    cluster = _BuildCluster([_build_obj(1), _build_obj(2)])

    assert _builder().trigger(cluster, "hello", "payments") is True

    kind, name, body = cluster.patched[0]
    assert (kind, name) == (ResourceKind.KPACK_BUILD, "fn-hello-payments-build-2")
    assert kpack.BUILD_TRIGGER_ANNOTATION in body["metadata"]["annotations"]
    assert len(cluster.patched) == 1  # one request, one build


def test_triggering_an_image_that_has_never_built_is_not_a_failure():
    """Applying the Image is itself what starts that build; there is nothing to annotate."""
    cluster = _BuildCluster([])

    assert _builder().trigger(cluster, "hello", "payments") is False
    assert cluster.patched == []


class _RebuildCluster:
    """An _ApplyCluster that also serves kpack Builds and records patches."""

    def __init__(self, existing, secrets=None, builds=(), site="site-a"):
        from tests.test_auth_and_deployer import _ApplyCluster

        self._inner = _ApplyCluster(site, existing, secrets)
        self._builds = list(builds)
        self.patched = []
        self.site = site
        self.name = site

    def __getattr__(self, item):
        return getattr(self._inner, item)  # applied/deleted/apply/delete

    def get(self, kind, name=None, label_selector=None, namespace=None):
        from common.cluster import ResourceKind

        if kind == ResourceKind.KPACK_BUILD:
            return list(self._builds)
        return self._inner.get(kind, name, label_selector, namespace)

    def patch(self, kind, name, body):
        self.patched.append((kind, name, body))
        return {}


def _deployed_ksvc(**over):
    """A KSVC as a cluster hands it back: with the uid an ownerReference needs."""
    ksvc = _ksvc(**over)
    ksvc["metadata"] = {**ksvc["metadata"], "uid": "uid-hello-payments"}
    return ksvc


def _build_cluster(**over):
    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    kwargs = dict(
        existing={
            "hello-payments": _deployed_ksvc(branch="release", path="services/api", version="3.11")
        },
        secrets={"hello-payments-git": stored},
        builds=[_build_obj(1)],
    )
    kwargs.update(over)
    return _RebuildCluster(**kwargs)


class _TriggeringBuilder(_RecordingBuilder):
    """A _RecordingBuilder that also records the trigger the engine asks for."""

    def __init__(self, state=None):
        super().__init__(state)
        self.triggered = []

    def trigger(self, cluster, name, group):
        self.triggered.append((cluster.site, name, group))
        return True


def _build_service(clusters, builder, runtimes=None, **kwargs):
    """A FunctionService whose runtime offers versions, as a deployed one has."""
    from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec
    from api.services.function import FunctionService
    from tests.test_auth_and_deployer import _workload_service

    registry = runtimes or RuntimeRegistry(
        [
            RuntimeSpec(
                name="python",
                builder="python",
                versionEnv="BP_CPYTHON_VERSION",
                defaultVersion="3.12",
                versions=["3.11", "3.12"],
            )
        ]
    )
    return FunctionService(_workload_service(clusters, builder=builder, **kwargs), registry)


async def _run_build(svc, group="payments", name="hello"):
    from fastapi import BackgroundTasks

    background = BackgroundTasks()
    body = await svc.accept_build(group, name, _principal(), background)
    await background()  # the 202 is returned first; this is the work behind it
    return body


async def test_build_builds_the_source_the_function_already_has():
    """No inputs are accepted, so they are read back off the workload itself.

    The same reconstruction a site that has never built the function does after a
    switchover: annotations for the source, the workload's own Secret for the token.
    """
    cluster = _build_cluster()
    builder = _TriggeringBuilder()

    await _run_build(_build_service({"site-a": cluster}, builder))

    assert builder.calls == 1
    req = builder.reqs[0]
    assert req.git_url == "https://git.internal/payments/hello.git"
    assert (req.branch, req.path, req.runtime, req.version) == (
        "release",
        "services/api",
        "python",
        "3.11",
    )
    # never re-supplied by the caller: a rebuild takes no body at all
    assert req.git_token == "ghp_stored"
    # the branch head, not a pinned commit - pinning one is the webhook's job
    assert req.revision is None and req.build_revision == "release"


async def test_build_applies_the_build_and_then_triggers_it():
    """Order matters: applying first is what makes a site with no Image build at all."""
    from tests.test_auth_and_deployer import _applied_kind

    cluster = _build_cluster()
    builder = _TriggeringBuilder()

    await _run_build(_build_service({"site-a": cluster}, builder))

    assert len(_applied_kind(cluster, "Image")) == 1
    assert len(_git_secrets(cluster)) == 1  # the site that clones needs the token
    assert builder.triggered == [("site-a", "hello", "payments")]


async def test_build_never_writes_the_workload():
    """Nothing about the desired state changes, so nothing about it is applied.

    The running revision keeps serving the image it already resolved; the new
    digest reaches it the way one from any other kpack-started build does
    (docs/BUILDING.md - Ownership: API vs Build Service).
    """
    from tests.test_auth_and_deployer import _applied_kind

    cluster = _build_cluster()

    await _run_build(_build_service({"site-a": cluster}, _TriggeringBuilder()))

    assert _applied_kind(cluster, "Service") == []
    assert _applied_kind(cluster, "DomainMapping") == []
    assert cluster.deleted == []  # and nothing is pruned out from under it


async def test_a_rebuilt_functions_build_objects_stay_owned_by_its_ksvc():
    """Re-applying them unowned would strand an Image that rebuilds a deleted function."""
    from tests.test_auth_and_deployer import _applied_kind

    cluster = _build_cluster()

    await _run_build(_build_service({"site-a": cluster}, _TriggeringBuilder()))

    owners = _applied_kind(cluster, "Image")[0]["metadata"]["ownerReferences"]
    assert [(o["kind"], o["name"]) for o in owners] == [("Service", "hello-payments")]


async def test_build_on_a_site_that_runs_no_copy_applies_the_build_unowned():
    """The build belongs to the local site; the KSVC may not be there at all.

    An ownerReference must name an owner in the same cluster, so there is none to
    give. ``delete`` reclaims these by name, as it does for a function targeted
    away from the local site.
    """
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    local = _RebuildCluster(existing={}, secrets={"hello-payments-git": stored}, builds=[])
    # the token is on every site, which is what lets any of them build
    # (docs/BUILDING.md - Active/Active Behaviour)
    remote = _ApplyCluster(
        "site-b", {"hello-payments": _deployed_ksvc()}, secrets={"hello-payments-git": stored}
    )
    svc = _build_service(
        {"site-a": local, "site-b": remote}, _TriggeringBuilder(), local_site="site-a"
    )

    await _run_build(svc)

    image = _applied_kind(local, "Image")[0]
    assert "ownerReferences" not in image["metadata"]
    assert _applied_kind(remote, "Image") == []  # one site builds, and it is the local one


async def test_build_without_a_stored_token_is_rejected_before_the_202():
    """Nothing can clone without it, and a rebuild has no body to supply one in."""
    from fastapi import BackgroundTasks

    from common.errors import ValidationError

    cluster = _build_cluster(secrets={})  # the git Secret is gone
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())
    background = BackgroundTasks()

    with pytest.raises(ValidationError, match="no git token is stored"):
        await svc.accept_build("payments", "hello", _principal(), background)
    assert background.tasks == []  # nothing was scheduled behind a 202


async def test_build_of_a_runtime_that_left_the_configmap_is_rejected_before_the_202():
    """A 400 now, not a build that fails minutes later and reads as a broken build."""
    from fastapi import BackgroundTasks

    from common.errors import ValidationError
    from tests.conftest import runtime_registry

    cluster = _build_cluster()
    # the function was built with "python"; the platform now offers only "go"
    svc = _build_service(
        {"site-a": cluster}, _TriggeringBuilder(), runtimes=runtime_registry(names=("go",))
    )

    with pytest.raises(ValidationError, match="unsupported runtime"):
        await svc.accept_build("payments", "hello", _principal(), BackgroundTasks())


async def test_build_is_accepted_as_pending_with_a_status_url():
    """Same 202 contract as create and update, so a client polls one place."""
    from fastapi import BackgroundTasks

    cluster = _build_cluster()
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())

    body = await svc.accept_build("payments", "hello", _principal(), BackgroundTasks())

    assert body.overallStatus == "Pending"
    assert body.statusUrl == "/api/v1/groups/payments/functions/hello"
    # the inputs it will build, echoed back - the request sent none of its own
    assert (body.runtime, body.branch, body.path, body.version) == (
        "python",
        "release",
        "services/api",
        "3.11",
    )
    assert body.hostname == "hello-payments.ex.com"  # the host it already serves on


async def test_a_container_of_the_same_name_cannot_be_rebuilt():
    """``{name}-{group}`` is shared by both offerings, so the object may not be a function.

    Hidden as a 404 rather than refused, matching every other read: the answer
    must not confirm that something else holds the name.
    """
    from fastapi import BackgroundTasks

    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.errors import NotFoundError

    container = build_ksvc(
        name="hello-payments",
        group="payments",
        owner="alice",
        image="reg/x:1",
        offering="container",
        host="hello-payments.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    cluster = _build_cluster(existing={"hello-payments": container})
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())

    with pytest.raises(NotFoundError):
        await svc.accept_build("payments", "hello", _principal(), BackgroundTasks())


async def test_build_of_a_workload_with_no_stored_source_is_rejected():
    """The annotations are the only record of what to build; without them there is none.

    Nothing can be reconstructed and there is no body to reconstruct it from, so
    the caller is told which inputs to send on a `PUT` rather than being handed a
    202 that fails minutes later.
    """
    from fastapi import BackgroundTasks

    from api.models.common import Scaling
    from api.services.manifests.ksvc import build_ksvc
    from common.errors import ValidationError

    unstamped = build_ksvc(  # a function KSVC carrying no build metadata
        name="hello-payments",
        group="payments",
        owner="alice",
        image="reg/fn:old",
        offering="function",
        host="hello-payments.ex.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        size="small",
    )
    cluster = _build_cluster(existing={"hello-payments": unstamped})
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())
    background = BackgroundTasks()

    with pytest.raises(ValidationError, match="gitRepo, branch, runtime"):
        await svc.accept_build("payments", "hello", _principal(), background)
    assert background.tasks == []


# ----------------------------------------------------------- build history


def test_the_image_bounds_its_own_build_history():
    """Unset is not unbounded: kpack's own default is 10 successful and 10 failed.

    A Build owns its pod, so 20 per function is 20 completed pods per function -
    invisible at ten functions, the whole namespace at three hundred.
    """
    settings = _settings(build={"registry_secret": "reg-creds", "success_history_limit": 2})
    image = _by_kind(_manifests(_builder(settings))[1], "Image")

    assert image["spec"]["successBuildHistoryLimit"] == 2
    assert image["spec"]["failedBuildHistoryLimit"] == 3  # the default, kept


def test_the_history_limits_are_the_same_on_every_apply():
    """A constant from configuration, so it converges like the rest of the spec.

    A value that moved per apply would be a nonce, and kpack would rebuild on it
    (docs/BUILDING.md - Convergence rules).
    """
    builder = _builder()
    first = _by_kind(_manifests(builder)[1], "Image")
    second = _by_kind(_manifests(builder)[1], "Image")

    assert first["spec"] == second["spec"]
    assert first["spec"]["successBuildHistoryLimit"] == 3
    assert first["spec"]["failedBuildHistoryLimit"] == 3


# --------------------------------------------------------- registry layout


def _layout_settings(**registry):
    base = dict(url="registry.internal", organization="acme")
    base.update(registry)
    return _settings(registry=base)


def test_the_builder_repository_prefixes_the_function_image_and_its_cache():
    """One root for everything the platform builds: the Builders and the functions.

    A function cannot collide with a Builder - a Builder is one path component
    below the base (`base/python`) and a function is two (`base/{group}/{name}`).
    """
    builder = _builder(_layout_settings(repository="serverless/builders"))

    assert builder.image_ref(_request()) == (
        "registry.internal/acme/serverless/builders/payments/hello:main"
    )
    assert builder.cache_ref(_request()) == (
        "registry.internal/acme/serverless/builders/payments/hello_cache:latest"
    )


def test_an_unset_repository_leaves_the_layout_exactly_as_it_was():
    """The prefix is optional, so an install that never sets it is unaffected."""
    builder = _builder(_layout_settings())

    assert builder.image_ref(_request()) == "registry.internal/acme/payments/hello:main"
    assert builder.cache_ref(_request()) == "registry.internal/acme/payments/hello_cache:latest"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("reg/acme/team/app:main", "reg/acme/team/app"),
        ("reg/acme/team/app@sha256:" + "a" * 64, "reg/acme/team/app"),
        ("reg/acme/team/app:main@sha256:" + "a" * 64, "reg/acme/team/app"),
        # the host's port is not a tag separator
        ("reg.internal:5000/team/app:main", "reg.internal:5000/team/app"),
        ("reg.internal:5000/team/app", "reg.internal:5000/team/app"),
    ],
)
def test_repository_of_ignores_the_tag_and_the_digest(image, expected):
    from common.names import repository_of

    assert repository_of(image) == expected


async def test_a_moved_registry_layout_is_adopted_by_the_next_update():
    """The layout is configuration, so nothing about the function marks the move.

    Left unnoticed the workload would point at a repository nothing pushes to,
    permanently: every later comparison is against the same stale value.
    """
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    class _MovedBuilder(_RecordingBuilder):
        def image_ref(self, req):
            return "reg/acme/serverless/builders/payments/hello:main"

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        # deployed under the old layout, at a digest a finished build resolved
        "site-a",
        {"hello-payments": _ksvc(image="reg/acme/payments/hello@sha256:" + "a" * 64)},
        secrets={"hello-payments-git": stored},
    )
    await _function_service({"site-a": cluster}, _MovedBuilder()).update(
        "payments",
        "hello",
        # every build input identical to what is stored: a config-only edit
        FunctionUpdate(gitRepo="https://git.internal/payments/hello.git", runtime="python"),
        _principal(),
    )

    ksvc = _applied_kind(cluster, "Service")[0]
    assert extract_image(ksvc) == "reg/acme/serverless/builders/payments/hello:main"


async def test_a_config_only_update_under_an_unchanged_layout_keeps_the_digest():
    """The move must be read off the repository alone, never the tag or digest.

    Comparing whole references would make every config edit rewrite a resolved
    digest back to the branch tag and spawn a revision for nothing.
    """
    from api.models.function import FunctionUpdate
    from api.services.state.ksvc_state import extract_image
    from tests.test_auth_and_deployer import _applied_kind, _ApplyCluster

    digest = "reg/acme/payments/hello@sha256:" + "b" * 64

    class _SameLayout(_RecordingBuilder):
        def image_ref(self, req):
            return "reg/acme/payments/hello:main"

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _ApplyCluster(
        "site-a", {"hello-payments": _ksvc(image=digest)}, secrets={"hello-payments-git": stored}
    )
    await _function_service({"site-a": cluster}, _SameLayout()).update(
        "payments",
        "hello",
        FunctionUpdate(gitRepo="https://git.internal/payments/hello.git", runtime="python"),
        _principal(),
    )

    assert extract_image(_applied_kind(cluster, "Service")[0]) == digest


# ----------------------------------------------------------- build edge cases


async def test_stored_inputs_that_no_longer_validate_are_a_400_not_a_500():
    """Rebuild is the only path building a BuildRequest from stored state.

    A hand-edited annotation, or a rule tightened since the function was created,
    fails the dataclass's own validation. Untranslated that reaches the catch-all
    handler as a 500 with a fixed message, hiding an actionable problem.
    """
    from fastapi import BackgroundTasks

    from api.models.common import ANNOTATION_GIT_PATH
    from common.errors import ValidationError

    ksvc = _deployed_ksvc()
    ksvc["metadata"]["annotations"][ANNOTATION_GIT_PATH] = "../etc"
    cluster = _build_cluster(existing={"hello-payments": ksvc})
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())

    with pytest.raises(ValidationError, match="stored build inputs are not valid"):
        await svc.accept_build("payments", "hello", _principal(), BackgroundTasks())


async def test_a_second_rebuild_annotates_whatever_build_is_latest_by_then():
    """Two requests are two builds - asking twice is what asking twice means.

    The trigger is read off the latest Build, so the second request must find the
    one kpack created for the first, not re-annotate a Build it has moved past.
    """
    cluster = _build_cluster()
    builder = _builder()

    assert builder.trigger(cluster, "hello", "payments") is True
    cluster._builds.append(_build_obj(2))  # kpack made one from that trigger
    assert builder.trigger(cluster, "hello", "payments") is True

    assert [name for _, name, _ in cluster.patched] == [
        "fn-hello-payments-build-1",
        "fn-hello-payments-build-2",
    ]


def test_the_history_limit_never_prunes_the_build_the_trigger_needs():
    """The two features interact: a rebuild annotates the latest retained Build.

    A limit of zero would leave nothing to annotate and make every rebuild a
    silent no-op, so the floor is one rather than a free-form integer.
    """
    import pydantic

    from common.config import BuildConfig

    with pytest.raises(pydantic.ValidationError):
        BuildConfig(success_history_limit=0)
    assert BuildConfig(success_history_limit=1).success_history_limit == 1


async def test_build_is_refused_for_a_group_the_caller_is_not_in():
    """Authorization is the engine's, and it runs before anything is read."""
    from fastapi import BackgroundTasks

    from api.auth.claims import Principal
    from common.errors import ForbiddenError

    svc = _build_service({"site-a": _build_cluster()}, _TriggeringBuilder())
    outsider = Principal(subject="u", username="mallory", groups=["other"])

    with pytest.raises(ForbiddenError):
        await svc.accept_build("payments", "hello", outsider, BackgroundTasks())


async def test_a_rebuild_that_cannot_reach_the_local_site_does_not_fail_the_202():
    """The 202 is already sent, so the failure belongs in the log and the status.

    `run` is what swallows it; letting it escape would crash the background task
    and lose the reason with it.
    """
    from fastapi import BackgroundTasks

    class _DownLocal(_RebuildCluster):
        def apply(self, manifest):
            raise RuntimeError("site down")

    stored = secret_svc.build_git_secret("hello-payments-git", {}, "ghp_stored")
    cluster = _DownLocal(
        existing={"hello-payments": _deployed_ksvc()},
        secrets={"hello-payments-git": stored},
        builds=[_build_obj(1)],
    )
    svc = _build_service({"site-a": cluster}, _TriggeringBuilder())

    background = BackgroundTasks()
    body = await svc.accept_build("payments", "hello", _principal(), background)
    assert body.overallStatus == "Pending"
    await background()  # must not raise
