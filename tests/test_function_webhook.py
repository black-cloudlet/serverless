"""The git webhook: the token, what a push builds, and what it must not touch.

A push is an *unauthenticated* caller reaching the build endpoint, so most of
these cover the shape of "no": which deliveries are refused (401), which are
acknowledged and ignored (200), and what the one that builds may not change.
The invariant behind nearly every case: a push moves the **commit**, never the
revision, the tag derived from it, or the workload (docs/FUNCTIONS.md - Git
webhook).
"""

from __future__ import annotations

import base64

import pytest
from cloudlet_apis.auth import Principal
from cloudlet_apis.errors import UnauthenticatedError
from fastapi import BackgroundTasks

from api.models.common import ANNOTATION_GIT_COMMIT
from api.models.webhook import GitLabPushEvent, WebhookOutcome
from api.services.manifests import secrets as secret_svc
from common.cluster import ResourceKind
from common.errors import NotFoundError
from common.names import same_repository
from tests.test_kpack_build import _RebuildCluster

pytestmark = pytest.mark.anyio

GIT_URL = "https://git.internal/payments/hello.git"
SHA = "9f2c1ab2b3c4d5e6f708192a3b4c5d6e7f809012"
OTHER_SHA = "c4f81de2b3c4d5e6f708192a3b4c5d6e7f809012"
TOKEN = "wh-token-value"  # noqa: S105 - a fixture value, not a credential


def _push(ref="refs/heads/main", after=SHA, url=GIT_URL, kind="push", checkout=None):
    return GitLabPushEvent(
        object_kind=kind,
        ref=ref,
        after=after,
        checkout_sha=checkout,
        project={"git_http_url": url},
        # Fields the platform does not read, present because GitLab sends them
        # and a delivery must not fail on one it does not know.
        commits=[{"id": after, "message": "x"}],
        user_username="dev",
    )


def _cluster(*, revision="main", commit=None, token=TOKEN, region="region-a"):
    """A region running `hello`, with a webhook token and maybe a pinned commit."""
    from tests.test_kpack_build import _build_obj, _deployed_ksvc, _RebuildCluster

    ksvc = _deployed_ksvc(revision=revision, path="services/api", version="3.11")
    if commit:
        ksvc["metadata"]["annotations"][ANNOTATION_GIT_COMMIT] = commit
    secrets = {"hello-git": secret_svc.build_git_secret("hello-git", {}, "ghp_stored")}
    if token is not None:
        secrets["hello-webhook"] = secret_svc.build_webhook_secret("hello-webhook", {}, token)
    return _RebuildCluster(
        existing={"hello": ksvc}, secrets=secrets, builds=[_build_obj(1)], region=region
    )


def _service(clusters, builder=None):
    from tests.test_kpack_build import _build_service, _TriggeringBuilder

    return _build_service(clusters, builder or _TriggeringBuilder())


async def _deliver(svc, event, *, token=TOKEN, kind="Push Hook", group="payments", name="hello"):
    background = BackgroundTasks()
    body = await svc.accept_webhook(group, name, token, event, kind, background)
    await background()  # the 202 comes first; this is the work behind it
    return body


def _stamped(cluster):
    """The commit annotations patched onto the KSVC, in order."""
    return [
        body["metadata"]["annotations"][ANNOTATION_GIT_COMMIT]
        for kind, _, body in cluster.patched
        if kind == ResourceKind.KNATIVE_SERVICE
        and ANNOTATION_GIT_COMMIT in (body.get("metadata") or {}).get("annotations", {})
    ]


# ------------------------------------------------------------------ the token


async def test_a_wrong_token_is_rejected_and_builds_nothing():
    cluster = _cluster()
    svc = _service({"region-a": cluster})

    with pytest.raises(UnauthenticatedError):
        await _deliver(svc, _push(), token="not-the-token")

    assert cluster.applied == []


async def test_a_function_with_no_stored_token_refuses_every_push():
    """Nothing to compare against is not "anything matches"."""
    svc = _service({"region-a": _cluster(token=None)})

    with pytest.raises(UnauthenticatedError):
        await _deliver(svc, _push(), token="anything")


async def test_a_function_that_does_not_exist_answers_the_same_as_a_wrong_token():
    """A 401, not a 404: a 404 would make the endpoint an oracle for which
    functions a group has, answerable by anyone who can reach the API."""
    svc = _service({"region-a": _cluster()})

    with pytest.raises(UnauthenticatedError):
        await _deliver(svc, _push(), name="no-such-function")


async def test_the_token_is_compared_in_constant_time():
    """Guards the comparison itself, which a `==` would silently satisfy."""
    import inspect

    from api.services import function as function_svc

    source = inspect.getsource(function_svc.FunctionService.accept_webhook)
    assert "hmac.compare_digest" in source


# --------------------------------------------------- deliveries that are ignored


@pytest.mark.parametrize(
    ("event", "kind", "expected"),
    [
        (_push(ref="refs/heads/develop"), "Push Hook", "is not this function's revision"),
        (_push(ref="refs/tags/v1.0.0"), "Push Hook", "is not a branch"),
        (_push(after="0" * 40), "Push Hook", "ref was deleted"),
        (_push(url="https://git.internal/other/repo.git"), "Push Hook", "different repository"),
        (_push(kind="tag_push"), "Tag Push Hook", "is not handled"),
        (_push(after="not-a-sha"), "Push Hook", "no usable commit"),
        (None, "Push Hook", "not a push event"),
    ],
)
async def test_a_delivery_that_is_not_this_functions_is_acknowledged_not_failed(
    event, kind, expected
):
    """Ignored deliveries answer 200, because a 4xx disables the hook.

    "This push is not mine" is the ordinary case where several functions build
    from one repository, so it must read as success or the hook stops working
    for the pushes that *are* mine.
    """
    cluster = _cluster()
    svc = _service({"region-a": cluster})

    outcome = await _deliver(svc, event, kind=kind)

    assert isinstance(outcome, WebhookOutcome)
    assert outcome.accepted is False
    assert expected in outcome.reason
    # ...and nothing was written: no build, and no pin.
    assert cluster.applied == []
    assert _stamped(cluster) == []


async def test_a_function_pinned_to_a_tag_or_a_commit_ignores_every_branch_push():
    """Asking for a tag or a SHA means "stay here", and no push moves it.

    Not special-cased: the match is `pushed branch == revision`, and neither a
    tag nor a SHA is a branch name.
    """
    for revision in ("v1.2.0", SHA):
        cluster = _cluster(revision=revision)
        outcome = await _deliver(_service({"region-a": cluster}), _push())

        assert outcome.accepted is False
        assert cluster.applied == []


async def test_a_repository_url_spelled_differently_still_matches():
    """A provider need not spell the URL the way the caller did."""
    assert same_repository(GIT_URL, "https://git.internal/payments/hello")
    assert same_repository(GIT_URL, "https://GIT.INTERNAL/payments/hello.git/")
    assert same_repository("https://u:p@git.internal/payments/hello.git", GIT_URL)
    # ...but the path is case-sensitive: folding it would let one repository's
    # push build another's function.
    assert not same_repository(GIT_URL, "https://git.internal/Payments/hello.git")
    assert not same_repository(GIT_URL, "https://git.internal:8443/payments/hello.git")
    assert not same_repository(GIT_URL, "")


# ------------------------------------------------------------- the build itself


async def test_a_push_to_the_revision_builds_that_commit():
    from tests.test_kpack_build import _TriggeringBuilder

    cluster = _cluster()
    builder = _TriggeringBuilder()

    body = await _deliver(_service({"region-a": cluster}, builder), _push())

    req = builder.reqs[0]
    assert req.commit == SHA
    # what kpack is told to build...
    assert req.build_revision == SHA
    # ...while the revision - and so the image tag - is untouched
    assert req.revision == "main"
    assert body.revision == "main"
    assert body.commit == SHA
    assert body.status == "Pending"


async def test_checkout_sha_wins_over_after():
    from tests.test_kpack_build import _TriggeringBuilder

    builder = _TriggeringBuilder()
    await _deliver(
        _service({"region-a": _cluster()}, builder),
        _push(after=SHA, checkout=OTHER_SHA),
    )

    assert builder.reqs[0].commit == OTHER_SHA


async def test_a_push_never_triggers_a_build_on_top_of_the_one_it_asked_for():
    """The changed revision *is* the spec change kpack builds from.

    A trigger too would be a second build - and, being a nonce, one per replica
    that handled the delivery (docs/BUILDING.md - Convergence rules).
    """
    from tests.test_kpack_build import _TriggeringBuilder

    builder = _TriggeringBuilder()
    await _deliver(_service({"region-a": _cluster()}, builder), _push())

    assert builder.triggered == []


async def test_the_commit_is_stamped_on_the_workload_in_every_region():
    """Without the pin, the next write would re-compose the revision and rebuild."""
    a, b = _cluster(region="region-a"), _cluster(region="region-b")

    await _deliver(_service({"region-a": a, "region-b": b}), _push())

    assert _stamped(a) == [SHA]
    assert _stamped(b) == [SHA]


async def test_the_pin_is_metadata_only_so_no_knative_revision_is_cut():
    """A pin says which source to compile next, not which image to run.

    In `spec.template` it would spawn a revision of the code already running,
    minutes before the real one arrives from the build controller.
    """
    cluster = _cluster()

    await _deliver(_service({"region-a": cluster}), _push())

    patch = next(b for k, _, b in cluster.patched if k == ResourceKind.KNATIVE_SERVICE)
    assert set(patch) == {"metadata"}


async def test_a_push_writes_no_ksvc_at_all():
    """Only build objects. The running revision keeps serving until the digest lands."""
    cluster = _cluster()

    await _deliver(_service({"region-a": cluster}), _push())

    assert [m["kind"] for m in cluster.applied if m["kind"] == "Service"] == []


async def test_the_build_is_owned_by_the_function_not_by_a_synthetic_user():
    """A push has no caller, so the build objects take the function's own owner."""
    from tests.test_kpack_build import _TriggeringBuilder

    builder = _TriggeringBuilder()
    await _deliver(_service({"region-a": _cluster()}, builder), _push())

    assert builder.reqs[0].owner == "alice"


async def test_a_redelivery_of_the_same_push_is_idempotent():
    """A provider retries, and two replicas may both take one push.

    Both apply the same commit, so it converges by data rather than a lease:
    kpack sees an unchanged spec and builds nothing.
    """
    from tests.test_kpack_build import _TriggeringBuilder

    cluster = _cluster()
    builder = _TriggeringBuilder()
    svc = _service({"region-a": cluster}, builder)

    await _deliver(svc, _push())
    first = [m for m in cluster.applied]
    await _deliver(svc, _push())

    assert cluster.applied[len(first) :] == first
    assert builder.triggered == []


# ------------------------------------------- returning to the revision's head


async def test_a_rebuild_returns_a_pinned_function_to_its_revisions_head():
    """`POST .../build` builds what the caller asked for, not where a push left it."""
    from tests.test_kpack_build import _run_build, _TriggeringBuilder

    cluster = _cluster(commit=SHA)
    builder = _TriggeringBuilder()

    await _run_build(_service({"region-a": cluster}, builder))

    req = builder.reqs[0]
    assert req.commit is None and req.build_revision == "main"
    # the pin is cleared with an explicit null, which removes the annotation
    # whichever field manager owns it
    patch = next(b for k, _, b in cluster.patched if k == ResourceKind.KNATIVE_SERVICE)
    assert patch["metadata"]["annotations"][ANNOTATION_GIT_COMMIT] is None


async def test_a_rebuild_of_a_pinned_function_still_triggers():
    """kpack decides from the *resolved* source, not from the spec text.

    Clearing a pin that still names the revision's head resolves to the commit
    already built, so the apply alone can produce nothing - and a rebuild that
    silently does nothing (the "retry a failed push-build" case) is worse than
    the second build a spec change plus a trigger can cost.
    """
    from tests.test_kpack_build import _run_build, _TriggeringBuilder

    builder = _TriggeringBuilder()
    await _run_build(_service({"region-a": _cluster(commit=SHA)}, builder))

    assert [region for region, *_ in builder.triggered] == ["region-a"]


async def test_a_rebuild_of_an_unpinned_function_still_triggers_as_it_always_did():
    """With nothing to clear the apply changes nothing, so the nonce is what builds."""
    from tests.test_kpack_build import _run_build, _TriggeringBuilder

    cluster = _cluster()
    builder = _TriggeringBuilder()

    await _run_build(_service({"region-a": cluster}, builder))

    assert [region for region, *_ in builder.triggered] == ["region-a"]
    assert _stamped(cluster) == []  # no pin written, and none to clear


# ----------------------------------------------------------------- the token API


async def test_rotate_replaces_the_token_in_every_region_and_returns_it():
    a, b = _cluster(region="region-a"), _cluster(region="region-b")
    svc = _service({"region-a": a, "region-b": b})

    view = await svc.rotate_webhook(
        "payments", "hello", Principal(subject="u", username="alice", groups=["payments"])
    )

    assert view.token != TOKEN
    for cluster in (a, b):
        secret = next(m for m in cluster.applied if m["kind"] == "Secret")
        assert secret["metadata"]["name"] == "hello-webhook"
    # the URL is the build endpoint, absolute where the API knows its own origin
    assert view.url.endswith("/v1/groups/payments/functions/hello/build")


async def test_rotate_writes_no_workload_and_no_build():
    """A credential rotation deploys nothing and starts no build."""
    cluster = _cluster()
    svc = _service({"region-a": cluster})

    await svc.rotate_webhook(
        "payments", "hello", Principal(subject="u", username="alice", groups=["payments"])
    )

    assert {m["kind"] for m in cluster.applied} == {"Secret"}


async def test_the_old_token_stops_working_once_rotated():
    cluster = _cluster()
    svc = _service({"region-a": cluster})
    user = Principal(subject="u", username="alice", groups=["payments"])

    view = await svc.rotate_webhook("payments", "hello", user)
    # the fake serves what was applied, so the rotated Secret is what a push reads
    cluster._inner._secrets["hello-webhook"] = next(
        m for m in cluster.applied if m["kind"] == "Secret"
    )

    with pytest.raises(UnauthenticatedError):
        await _deliver(svc, _push(), token=TOKEN)
    assert (await _deliver(svc, _push(), token=view.token)).commit == SHA


# ------------------------------------------------- where the token comes from


async def test_a_create_returns_the_token_and_writes_it_to_every_region():
    """Minted before the 202, so the caller configures the hook from it.

    The Secret written in the background must be the token they were handed, or
    the hook they configured would never authenticate.
    """
    from tests.factories import _ApplyCluster
    from tests.test_kpack_build import _create_spec, _function_service, _RecordingBuilder

    clusters = {r: _ApplyCluster(r, {}) for r in ("region-a", "region-b")}
    svc = _function_service(clusters, _RecordingBuilder())
    background = BackgroundTasks()

    body = await svc.accept(
        "payments",
        _create_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
        background,
    )
    await background()

    assert body.webhook is not None
    for cluster in clusters.values():
        secret = next(
            m
            for m in cluster.applied
            if m["kind"] == "Secret" and m["metadata"]["name"] == "hello-webhook"
        )
        stored = base64.b64decode(secret["data"][secret_svc.WEBHOOK_TOKEN_KEY]).decode()
        assert stored == body.webhook.token


async def test_a_generated_token_is_not_guessable():
    seen = {secret_svc.new_webhook_token() for _ in range(100)}

    assert len(seen) == 100
    assert all(len(t) >= 40 for t in seen)


def test_the_token_never_appears_in_a_repr():
    """A credential must not ride along into a traceback or a log line."""
    from api.auth.deps import WebhookCaller
    from api.models.function import WebhookView

    view = WebhookView(url="https://api/x", token="s3cret-value")
    caller = WebhookCaller(token="s3cret-value", event="Push Hook")

    assert "s3cret" not in repr(view)
    assert "s3cret" not in repr(caller)


async def test_an_update_never_writes_the_webhook_secret():
    """Rotate is the token's only writer.

    A PUT that touched it could only either re-apply what it read - pointless -
    or mint a replacement it has no field to hand back, silently breaking a hook
    the caller had already configured.
    """
    from tests.test_kpack_build import _function_service, _RecordingBuilder

    cluster = _cluster()
    svc = _function_service({"region-a": cluster}, _RecordingBuilder())

    await svc.update(
        "payments",
        "hello",
        _update_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
    )

    assert [m for m in cluster.applied if m["metadata"]["name"] == "hello-webhook"] == []


async def test_an_update_of_a_function_with_no_token_still_writes_none():
    """The dangerous half: no token stored must not mean "mint one quietly"."""
    from tests.test_kpack_build import _function_service, _RecordingBuilder

    cluster = _cluster(token=None)
    svc = _function_service({"region-a": cluster}, _RecordingBuilder())

    await svc.update(
        "payments",
        "hello",
        _update_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
    )

    assert [m for m in cluster.applied if m["metadata"]["name"] == "hello-webhook"] == []


async def test_an_update_returns_a_pinned_function_to_its_revisions_head():
    """`commit` is not part of any desired state a caller sends, so a PUT clears it."""
    from tests.test_kpack_build import _function_service, _RecordingBuilder

    cluster = _cluster(commit=SHA)
    svc = _function_service({"region-a": cluster}, _RecordingBuilder())

    await svc.update(
        "payments",
        "hello",
        _update_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
    )

    ksvc = next(m for m in cluster.applied if m["kind"] == "Service")
    assert ANNOTATION_GIT_COMMIT not in ksvc["metadata"]["annotations"]
    # ...and the stored one is removed outright, rather than left to a
    # server-side apply that may not own the field the webhook's patch wrote
    patch = next(b for k, _, b in cluster.patched if k == ResourceKind.KNATIVE_SERVICE)
    assert patch["metadata"]["annotations"][ANNOTATION_GIT_COMMIT] is None


async def test_an_update_of_an_unpinned_function_patches_nothing():
    """No pin, no write: an ordinary update must not churn the workload."""
    from tests.test_kpack_build import _function_service, _RecordingBuilder

    cluster = _cluster()
    svc = _function_service({"region-a": cluster}, _RecordingBuilder())

    await svc.update(
        "payments",
        "hello",
        _update_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
    )

    assert _stamped(cluster) == []


def _update_spec(**over):
    from api.models.function import FunctionUpdate

    base = dict(
        gitRepo=GIT_URL,
        revision="main",
        path="services/api",
        runtime="python",
        version="3.11",
    )
    base.update(over)
    return FunctionUpdate(**base)


# ------------------------------------------------- the failures reviews found


async def test_the_minted_token_never_reaches_a_log_line():
    """`run_background` logs the callable and its string args on failure.

    A `functools.partial` renders bound arguments in its `repr`, and a plain
    positional would be logged as one of those strings - either way the
    credential lands in the log of any failed create.
    """
    from api.services.function import _with_webhook_token

    work = _with_webhook_token(lambda *a, **k: None, "s3cret-token")

    assert "s3cret" not in repr(work)
    assert "s3cret" not in getattr(work, "__name__", "")
    assert "s3cret" not in repr(getattr(work, "__name__", work))


async def test_a_non_ascii_token_is_a_401_not_a_500():
    """`hmac.compare_digest` raises on a non-ASCII `str`, and the header is
    caller-controlled: Starlette decodes it latin-1, so any byte >= 0x80 would
    turn a junk token into a server error."""
    svc = _service({"region-a": _cluster()})

    with pytest.raises(UnauthenticatedError):
        await _deliver(svc, _push(), token="tokén-with-non-ascii")


async def test_a_rotation_that_reached_no_region_is_not_reported_as_success():
    """The worst possible answer: the caller reconfigures the hook with a token
    no region will accept, while the one they replaced stays live."""
    from common.errors import RegionTotalFailure

    class _Broken:
        """A region that reads fine but refuses every write."""

        def __init__(self):
            self._inner = _cluster()
            self.region = self.name = "region-a"

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def apply(self, manifest, namespace=None):
            raise RuntimeError("region down")

    svc = _service({"region-a": _Broken()})

    with pytest.raises(RegionTotalFailure):
        await svc.rotate_webhook(
            "payments", "hello", Principal(subject="u", username="alice", groups=["payments"])
        )


async def test_a_rotation_that_only_reached_regions_without_the_workload_fails_too():
    """One region absent and one refusing the write stores the token nowhere."""
    from common.errors import RegionTotalFailure

    class _Broken:
        """A region that reads fine but refuses every write."""

        def __init__(self, region):
            self._inner = _cluster(region=region)
            self.region = self.name = region

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def apply(self, manifest, namespace=None):
            raise RuntimeError("region down")

    # region-a has no workload at all (a partial deploy, or a region added
    # since); region-b has it and cannot be written to.
    empty = _RebuildCluster(existing={}, region="region-a")
    svc = _service({"region-a": empty, "region-b": _Broken("region-b")})

    with pytest.raises(RegionTotalFailure):
        await svc.rotate_webhook(
            "payments", "hello", Principal(subject="u", username="alice", groups=["payments"])
        )


async def test_a_pin_that_could_be_cleared_nowhere_is_not_reported_as_cleared():
    """The same rule on the other write that uses the guard."""
    from common.errors import RegionTotalFailure

    class _Absent:
        """A region without the workload: the patch 404s."""

        def __init__(self, region):
            self._inner = _RebuildCluster(existing={}, region=region)
            self.region = self.name = region

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def patch(self, kind, name, body, namespace=None):
            raise NotFoundError(f"{kind.kind} '{name}' not found")

    class _Unpatchable:
        def __init__(self, region):
            self._inner = _cluster(region=region, commit=SHA)
            self.region = self.name = region

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def patch(self, kind, name, body, namespace=None):
            raise RuntimeError("region down")

    svc = _service({"region-a": _Absent("region-a"), "region-b": _Unpatchable("region-b")})

    with pytest.raises(RegionTotalFailure):
        await svc._engine.clear_commit("hello", "payments")


async def test_an_unbuildable_function_ignores_a_push_instead_of_failing_it():
    """A 4xx would make GitLab disable the hook for every later push too.

    A runtime retired from the ConfigMap is the realistic trigger: the function
    is temporarily unbuildable, but that must not cost it its webhook.
    """
    from api.services.builder.runtimes import RuntimeRegistry
    from tests.test_kpack_build import _build_service, _TriggeringBuilder

    cluster = _cluster()
    # a registry that no longer offers the runtime the function was built with
    svc = _build_service({"region-a": cluster}, _TriggeringBuilder(), runtimes=RuntimeRegistry([]))

    outcome = await _deliver(svc, _push())

    assert outcome.accepted is False
    assert "runtime" in outcome.reason
    assert cluster.applied == []


def test_a_repository_reached_over_http_and_https_is_one_repository():
    """GitLab's `git_http_url` need not use the scheme the caller registered;
    comparing schemes would silently ignore every push from such a server."""
    assert same_repository("http://git.internal/payments/hello.git", GIT_URL)
    assert same_repository("https://git.internal:443/payments/hello.git", GIT_URL)
    assert same_repository("http://git.internal:80/payments/hello", GIT_URL)
    # a non-default port still distinguishes a host
    assert not same_repository("https://git.internal:8443/payments/hello.git", GIT_URL)
