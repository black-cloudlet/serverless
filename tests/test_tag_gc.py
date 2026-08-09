"""Tag GC: the build controller pruning kpack's per-build tags.

Most of these are about what a sweep must *keep* - the branch tag, every tag on
the digest still serving, the newest builds - because the failure mode of a GC
is deleting something live, and about the logs: the whole feature runs in a
pod nobody watches, so its log lines are the only way an operator knows tags
are being reclaimed, skipped, or refused.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from common.config import SiteConfig
from common.errors import NotFoundError
from common.registry import TagInfo
from controller.config import ControllerSettings
from controller.gc import TagGC, digest_of, garbage, tag_of
from controller.reconciler import Reconciler

_LOGGER = "controller.gc"
_CLIENT_LOGGER = "common.registry"

REPO = "payments/hello"
TAG = f"registry.internal/{REPO}:main"

# Six builds' worth of history, the shape kpack leaves behind: the branch tag
# rides the newest digest, one b{n}.{date}.{time} tag per build accumulates,
# and a branch change once left a 'dev' tag pointing at an old digest.
_D = {n: f"sha256:{str(n) * 8}" for n in range(1, 7)}
_HISTORY = [
    {"name": "main", "manifest_digest": _D[6], "start_ts": 60},
    {"name": "b6.20260806.120000", "manifest_digest": _D[6], "start_ts": 60},
    {"name": "b5.20260805.120000", "manifest_digest": _D[5], "start_ts": 50},
    {"name": "b4.20260804.120000", "manifest_digest": _D[4], "start_ts": 40},
    {"name": "b3.20260803.120000", "manifest_digest": _D[3], "start_ts": 30},
    {"name": "dev", "manifest_digest": _D[2], "start_ts": 20},
    {"name": "b2.20260802.120000", "manifest_digest": _D[2], "start_ts": 20},
    {"name": "b1.20260801.120000", "manifest_digest": _D[1], "start_ts": 10},
]


def _tags(entries=_HISTORY):
    return [
        TagInfo(name=e["name"], digest=e["manifest_digest"], start_ts=e["start_ts"])
        for e in entries
    ]


def _settings(**overrides):
    values = dict(
        sites=[SiteConfig(name="central", cluster="central-0")],
        local_site="central",
        registry=dict(url="registry.internal", api_token="oauth-token"),
    )
    values.update(overrides)
    return ControllerSettings(**values)


def _image(tag=TAG, latest=f"registry.internal/{REPO}@{_D[6]}", workload="hello-payments"):
    """A kpack Image carrying what the GC reads: spec.tag and latestImage."""
    status = {"conditions": [{"type": "Ready", "status": "True"}]}
    if latest:
        status["latestImage"] = latest
    return {
        "metadata": {
            "name": "fn-hello-payments",
            "labels": {"serverless.platform/workload": workload},
        },
        "spec": {"tag": tag},
        "status": status,
    }


class _Quay:
    """A management API serving tag listings and recording tag deletes."""

    def __init__(self, tags_by_repo=None, list_status=200, delete_status=204):
        self._tags = tags_by_repo if tags_by_repo is not None else {REPO: list(_HISTORY)}
        self._list_status = list_status
        self._delete_status = delete_status
        self.listed: list[str] = []
        self.deleted: list[str] = []  # "repo:tag"
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        path = request.url.path.removeprefix("/api/v1/repository/")
        if request.method == "GET" and path.endswith("/tag/"):
            repo = path.removesuffix("/tag/")
            self.listed.append(repo)
            if self._list_status != 200:
                return httpx.Response(self._list_status)
            if repo not in self._tags:
                return httpx.Response(404)
            return httpx.Response(
                200, content=json.dumps({"tags": self._tags[repo], "has_additional": False})
            )
        if request.method == "DELETE" and "/tag/" in path:
            repo, _, tag = path.rpartition("/tag/")
            resp = httpx.Response(self._delete_status)
            if resp.is_success:
                self.deleted.append(f"{repo}:{tag}")
            return resp
        return httpx.Response(405)


def _gc(monkeypatch, quay: _Quay, settings=None, site="central") -> TagGC:
    transport = httpx.MockTransport(quay.handler)
    real = httpx.Client

    def client(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return TagGC(settings or _settings(), site)


# --------------------------------------------------------------------------- #
# the policy: what survives, what is garbage                                   #
# --------------------------------------------------------------------------- #


def _garbage(tags=None, protected_tags={"main"}, protected_digests={_D[6]}, keep=3):  # noqa: B006
    return garbage(
        tags if tags is not None else _tags(),
        protected_tags=set(protected_tags),
        protected_digests=set(protected_digests),
        keep=keep,
    )


def test_the_expected_tags_and_only_those_are_garbage():
    # newest 3 builds (b6, b5, b4) survive; the branch tag by name; the rest go
    assert [t.name for t in _garbage()] == [
        "b3.20260803.120000",
        "dev",
        "b2.20260802.120000",
        "b1.20260801.120000",
    ]


def test_the_branch_tag_survives_regardless_of_age():
    tags = [TagInfo("main", _D[1], 10), TagInfo("b9.20260809.120000", _D[2], 90)]
    # even pointing at an ancient digest, the name alone protects it: a create
    # deploys at the branch tag, and a switchover site rebuilds into it
    assert _garbage(tags, protected_digests=set(), keep=1) == []


def test_every_tag_on_the_serving_digest_survives():
    doomed = _garbage(protected_digests={_D[2]})
    # b2 and 'dev' both point at the still-serving digest; deleting the last
    # tag on a manifest lets the registry collect it out from under the KSVC
    assert [t.name for t in doomed] == ["b3.20260803.120000", "b1.20260801.120000"]


def test_protected_names_do_not_consume_keep_slots():
    # 'main' is always the newest tag - counted into keep, every sweep would
    # silently protect one build fewer than configured
    doomed = _garbage(keep=1)
    assert "b6.20260806.120000" not in {t.name for t in doomed}
    assert "b5.20260805.120000" in {t.name for t in doomed}


def test_a_tag_without_a_digest_is_never_deleted():
    tags = [TagInfo("mystery", "", 5), TagInfo("b1.20260801.120000", _D[1], 10)]
    # 'mystery' is the oldest, unprotected by name or digest - but with no
    # digest it cannot be proven safe, and the safe direction is to keep it
    assert _garbage(tags, keep=1) == []


def test_keep_zero_leaves_only_the_protected_tags():
    doomed = _garbage(keep=0)
    assert {t.name for t in doomed} == {
        "b5.20260805.120000",
        "b4.20260804.120000",
        "b3.20260803.120000",
        "dev",
        "b2.20260802.120000",
        "b1.20260801.120000",
    }


def test_no_tags_is_no_garbage():
    assert _garbage([]) == []


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (TAG, "main"),
        (f"registry.internal/{REPO}", None),
        (f"registry.internal/{REPO}@{_D[6]}", None),
        (f"registry.internal:5000/{REPO}:feature-login", "feature-login"),
    ],
)
def test_the_tag_half_of_a_reference(reference, expected):
    assert tag_of(reference) == expected


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (f"registry.internal/{REPO}@{_D[6]}", _D[6]),
        (TAG, None),
        (None, None),
    ],
)
def test_the_digest_half_of_a_reference(reference, expected):
    assert digest_of(reference) == expected


# --------------------------------------------------------------------------- #
# the sweep                                                                    #
# --------------------------------------------------------------------------- #


def test_a_sweep_prunes_old_build_tags_and_stale_branch_tags(monkeypatch):
    quay = _Quay()
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])

    assert sorted(quay.deleted) == [
        f"{REPO}:b1.20260801.120000",
        f"{REPO}:b2.20260802.120000",
        f"{REPO}:b3.20260803.120000",
        f"{REPO}:dev",
    ]


def test_only_the_image_repository_is_addressed_never_the_cache(monkeypatch):
    quay = _Quay(tags_by_repo={REPO: list(_HISTORY), f"{REPO}_cache": [{"name": "latest"}]})
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])

    # the cache reuses one 'latest' tag and does not accumulate; the GC derives
    # its target from spec.tag and must never widen to the cache beside it
    assert quay.listed == [REPO]
    assert not any(d.startswith(f"{REPO}_cache") for d in quay.deleted)


def test_a_sweep_is_paced_to_the_interval(monkeypatch):
    quay = _Quay()
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])
    first = quay.requests
    gc.maybe_sweep([_image()])

    # the second resync is minutes later; the GC works on the hours scale
    assert quay.requests == first


def test_gc_off_without_a_token_says_so_and_never_calls_the_registry(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay()
    gc = _gc(monkeypatch, quay, _settings(registry=dict(url="registry.internal", api_token="")))

    gc.maybe_sweep([_image()])

    assert quay.requests == 0
    assert any("tag GC off: no registry API token" in r.getMessage() for r in caplog.records)


def test_gc_disabled_by_configuration_says_so(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay()
    gc = _gc(monkeypatch, quay, _settings(gc_enabled=False))

    gc.maybe_sweep([_image()])

    assert quay.requests == 0
    assert any("tag GC off: disabled" in r.getMessage() for r in caplog.records)


def test_gc_announces_itself_on_at_startup(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _gc(monkeypatch, _Quay())
    # the counterpart of the off-lines: presence is stated, not deduced from silence
    assert any(
        "tag GC on: pruning old build tags in registry.internal" in r.getMessage()
        for r in caplog.records
    )


def test_a_foreign_host_tag_is_skipped_with_a_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay()
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image(tag=f"elsewhere.internal/{REPO}:main")])

    assert quay.requests == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("is not on registry.internal" in r.getMessage() for r in warnings)


def test_a_function_deleted_mid_sweep_is_skipped(monkeypatch):
    quay = _Quay(tags_by_repo={})  # every listing 404s
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])

    assert quay.deleted == []


def test_an_unreachable_registry_never_raises_into_the_loop(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)

    def refuse(request):
        raise httpx.ConnectError("registry down")

    quay = _Quay()
    quay.handler = refuse
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])  # must not raise

    assert any("sweep failed" in r.getMessage() for r in caplog.records)


def test_an_image_naming_no_tag_is_skipped(monkeypatch):
    quay = _Quay()
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([{"metadata": {"name": "fn-x"}, "spec": {}, "status": {}}])

    assert quay.requests == 0


def test_the_sweep_logs_a_per_function_verdict_and_a_summary(monkeypatch, caplog):
    """The log lines are the feature's UI; their content is contract, not style."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    caplog.set_level(logging.INFO, logger=_CLIENT_LOGGER)
    gc = _gc(monkeypatch, _Quay())

    gc.maybe_sweep([_image()])

    messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    # the per-function verdict: what happened, to whom, and what was spared
    assert any("'hello-payments': pruned 4 of 8 tag(s) in 'payments/hello'" in m for m in messages)
    # the sweep summary: the one line that says the pass ran and what it did
    assert any("swept 1 function repositories in 'central', pruned 4 tag(s)" in m for m in messages)
    # and each individual tag is named by the client, so a delete is traceable
    client_messages = [r.getMessage() for r in caplog.records if r.name == _CLIENT_LOGGER]
    assert any("deleted registry tag 'payments/hello:dev'" in m for m in client_messages)


def test_a_sweep_with_nothing_to_prune_stays_quiet_per_function(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay(tags_by_repo={REPO: list(_HISTORY[:4])})  # nothing beyond keep+protected
    gc = _gc(monkeypatch, quay)

    gc.maybe_sweep([_image()])

    messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert quay.deleted == []
    assert not any("pruned" in m and "of" in m for m in messages if "'hello-payments'" in m)
    # the summary still reports the pass, so "quiet" is never "did not run"
    assert any("swept 1 function repositories" in m for m in messages)


# --------------------------------------------------------------------------- #
# riding the reconcile loop                                                    #
# --------------------------------------------------------------------------- #


def test_resync_hands_its_listing_to_the_gc():
    class _Cluster:
        site = "central"

        def list_resources(self, kind, *, label_selector=None):
            return [_image()], "7"

        def get(self, kind, name=None, label_selector=None):
            raise NotFoundError(name or "")

    class _GC:
        def __init__(self):
            self.calls = []

        def maybe_sweep(self, images):
            self.calls.append(images)

    reconciler = object.__new__(Reconciler)
    reconciler._local = _Cluster()
    reconciler._gc = _GC()

    reconciler.resync()

    # the same listing the rollouts just used - being due costs no second LIST
    assert [i["metadata"]["name"] for i in reconciler._gc.calls[0]] == ["fn-hello-payments"]


def test_the_gc_factory_receives_the_resolved_site_name():
    seen = []
    # the chart may spell local_site as the CLUSTER name; the GC must still
    # resolve the site whose registry it prunes
    reconciler = Reconciler(_settings(local_site="central-0"), gc_factory=seen.append)
    try:
        assert seen == ["central"]
    finally:
        reconciler.close()


# --------------------------------------------------------------------------- #
# settings                                                                     #
# --------------------------------------------------------------------------- #


def test_gc_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERVERLESS_GC_ENABLED", "false")
    monkeypatch.setenv("SERVERLESS_GC_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("SERVERLESS_GC_KEEP_BUILDS", "5")

    settings = ControllerSettings()

    assert settings.gc_enabled is False
    assert settings.gc_interval_seconds == 3600
    assert settings.gc_keep_builds == 5


def test_a_zero_gc_interval_is_rejected():
    # it would sweep on every resync, hammering the registry's API
    with pytest.raises(ValueError):
        ControllerSettings(gc_interval_seconds=0)


def test_a_negative_keep_is_rejected():
    with pytest.raises(ValueError):
        ControllerSettings(gc_keep_builds=-1)
