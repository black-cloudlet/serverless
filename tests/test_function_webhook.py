"""The git webhook: the token, what a push builds, and what it must not touch.

A push is an *unauthenticated* caller reaching the build endpoint, so most of
what these cover is the shape of "no": which deliveries are refused outright
(401), which are acknowledged and ignored (200), and - for the one that does
build - what the platform refuses to let a push change. The invariant behind
almost every case: a push moves the **commit**, never the revision the caller
chose, the image tag derived from it, or anything else about the workload
(docs/FUNCTIONS.md - Git webhook).
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
from common.names import same_repository

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
        # Payload fields the platform does not read; present because GitLab
        # sends them and a delivery must not fail on one it does not know.
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
    """A 401, not a 404: the caller has proved nothing, so it learns nothing.

    A 404 here would turn the endpoint into an oracle for which functions a
    group has, answerable by anyone who can reach the API.
    """
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

    GitLab disables a webhook that keeps failing, so "this push is not mine" -
    the ordinary case in a repository several functions build from - has to
    read as success or the hook stops working for the pushes that *are* mine.
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
    """Asking for a tag or a SHA means "stay here", and a push cannot move it.

    Falls out of the revision rename rather than being special-cased: the match
    is `pushed branch == revision`, and a tag or a SHA is not a branch name.
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
    # ...but the path is case-sensitive: git forges distinguish these, and
    # treating them as one would let one repository build another's function.
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

    A trigger annotation as well would be a second build for one push - and,
    being a nonce, would produce one per API replica that handled the delivery
    (docs/BUILDING.md - Convergence rules).
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

    Writing it into `spec.template` would spawn a revision of the code already
    running, minutes before the real one arrives from the build controller.
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
    """A provider retries, and two API replicas may both take one push.

    Both apply the same commit, which converges by data rather than by a lease:
    kpack sees an unchanged spec the second time and builds nothing.
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


async def test_clearing_a_pin_is_the_spec_change_so_no_trigger_is_sent():
    from tests.test_kpack_build import _run_build, _TriggeringBuilder

    builder = _TriggeringBuilder()
    await _run_build(_service({"region-a": _cluster(commit=SHA)}, builder))

    assert builder.triggered == []


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
    """Minted before the 202, so the caller configures the hook from that response.

    The Secret written in the background must be the token they were handed, or
    the hook they configured from the create would never authenticate.
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


async def test_an_update_carries_the_token_to_a_region_the_create_did_not_reach():
    """An update targets every configured region, so the token has to travel too.

    A function created into one region and later updated must authenticate a
    push wherever it now runs - otherwise a switchover silently breaks its hook.
    """
    from tests.factories import _ApplyCluster
    from tests.test_kpack_build import _function_service, _RecordingBuilder

    a = _cluster(region="region-a")
    b = _ApplyCluster("region-b", {"hello": a._inner._existing["hello"]})
    svc = _function_service({"region-a": a, "region-b": b}, _RecordingBuilder())

    await svc.update(
        "payments",
        "hello",
        _update_spec(),
        Principal(subject="u", username="alice", groups=["payments"]),
    )

    for cluster in (a, b):
        secret = next(
            m
            for m in cluster.applied
            if m["kind"] == "Secret" and m["metadata"]["name"] == "hello-webhook"
        )
        assert base64.b64decode(secret["data"][secret_svc.WEBHOOK_TOKEN_KEY]).decode() == TOKEN


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
