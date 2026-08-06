"""Registry cleanup: the repositories a deleted function leaves behind."""

from __future__ import annotations

import logging

import httpx
import pytest

from api.services.builder.registry import (
    delete_function_repositories,
    moved_repositories,
    reclaim_moved_repositories,
)
from common.config import CommonSettings
from common.names import cache_repository, image_repository


def _settings(**registry):
    base = dict(url="registry.internal", api_token="oauth-token")
    base.update(registry)
    return CommonSettings(registry=base)


class _Quay:
    """A management API that records what was deleted."""

    def __init__(self, status=204):
        self._status = status
        self.deleted: list[str] = []
        self.tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "DELETE":
            return httpx.Response(405)
        self.tokens.append(request.headers.get("Authorization", ""))
        repo = request.url.path.removeprefix("/api/v1/repository/")
        resp = httpx.Response(self._status)
        if resp.is_success:
            self.deleted.append(repo)
        return resp


_LOGGER = "api.services.builder.registry"


def _records(caplog):
    """Only this module's records.

    caplog's handler sits on the root logger, so it collects every propagating
    record - httpx logs each request at INFO. Filtering by logger name keeps
    these assertions about what the cleanup path concluded, and independent of
    whatever else in the suite has lowered the root level.
    """
    return [r for r in caplog.records if r.name == _LOGGER]


def _run(monkeypatch, quay: _Quay, settings=None) -> None:
    transport = httpx.MockTransport(quay.handler)
    real = httpx.Client

    def client(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    delete_function_repositories((settings or _settings()).registry, "payments", "hello")


def test_both_the_image_and_the_cache_repository_are_deleted(monkeypatch):
    quay = _Quay()
    _run(monkeypatch, quay)
    # the cache is a separate repository, so deleting the image repo leaves it
    assert sorted(quay.deleted) == ["payments/hello", "payments/hello_cache"]


def test_the_repositories_come_from_config_not_from_the_request(monkeypatch):
    quay = _Quay()
    _run(monkeypatch, quay, _settings(organization="acme"))
    # with an organization the group is no longer the Quay namespace; the route
    # takes the whole {namespace}/{repository} path either way
    assert sorted(quay.deleted) == ["acme/payments/hello", "acme/payments/hello_cache"]


def test_the_oauth_token_is_sent_as_a_bearer(monkeypatch):
    quay = _Quay()
    _run(monkeypatch, quay)
    # a robot's basic credentials would not authenticate against /api/v1 at all
    assert quay.tokens == ["Bearer oauth-token"] * 2


def test_cleanup_is_skipped_without_a_token(monkeypatch):
    quay = _Quay()
    _run(monkeypatch, quay, _settings(api_token=""))
    # the token is what enables this; an install that never wires it is untouched
    assert quay.deleted == []


def test_cleanup_is_skipped_when_switched_off(monkeypatch):
    quay = _Quay()
    _run(monkeypatch, quay, _settings(delete_on_function_delete=False))
    assert quay.deleted == []


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_a_registry_that_refuses_never_fails_the_delete(monkeypatch, status):
    # the workload is already gone platform-wide; a leftover repository is not
    # worth reporting a function as undeleted
    _run(monkeypatch, _Quay(status=status))


def test_repository_paths_track_the_reference_convention():
    from common.build import BuildRequest, cache_reference, image_reference

    req = BuildRequest(
        name="hello",
        group="payments",
        git_url="https://git.internal/payments/hello.git",
        branch="main",
        git_token="ghp_tok",
        runtime="python",
        owner="alice",
    )
    image = image_repository("payments", "hello")
    cache = cache_repository("payments", "hello")
    # cleanup and the build now name the repository through the same functions,
    # so this pins that the references really are {base}/{repository}:{tag}
    assert image_reference("registry.internal/acme", req).startswith(
        f"registry.internal/acme/{image}:"
    )
    assert cache_reference("registry.internal/acme", req).startswith(
        f"registry.internal/acme/{cache}:"
    )


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_any_2xx_is_reported_as_deleted(monkeypatch, caplog, status):
    """Success is the whole 2xx class, not the codes we happened to have seen.

    Asserted on what the code *logged*, not on what the fake recorded: the
    response status changes nothing about the request that was already sent, so
    the log line is the only place the code's verdict is observable. Quay
    answers with 204 today, and enumerating that - plus the 200/202 added
    defensively - would report a registry answering 201 as a failed cleanup.
    """
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _run(monkeypatch, _Quay(status=status))

    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO, logging.INFO]
    assert "deleted registry repository" in records[0].getMessage()


def test_a_refused_delete_is_reported_as_a_warning(monkeypatch, caplog):
    """The counterpart: a non-2xx must not be logged as a successful cleanup."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    _run(monkeypatch, _Quay(status=403))

    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert "not authorized" in records[0].getMessage()


def test_cleanup_follows_the_repository_prefix_the_build_pushed_under(monkeypatch):
    """Delete addresses the same path the image reference hangs off, minus the host.

    Derived separately, a layout with `build.builderRepository` set would push to
    one repository and delete another - leaving every deleted function's content
    behind while reporting a clean delete.
    """
    quay = _Quay()
    _run(monkeypatch, quay, _settings(organization="acme", repository="serverless/builders"))

    assert sorted(quay.deleted) == [
        "acme/serverless/builders/payments/hello",
        "acme/serverless/builders/payments/hello_cache",
    ]


def test_the_prefix_skips_whichever_half_is_unset(monkeypatch):
    """A flat install has neither, and must not produce a leading or doubled slash."""
    quay = _Quay()
    _run(monkeypatch, quay, _settings(repository="fns"))

    assert sorted(quay.deleted) == ["fns/payments/hello", "fns/payments/hello_cache"]


# --------------------------------------------------------------------------- #
# reclaiming what a moved tag left behind                                       #
# --------------------------------------------------------------------------- #


def _reclaim(monkeypatch, quay: _Quay, previous_tag, settings=None) -> None:
    transport = httpx.MockTransport(quay.handler)
    real = httpx.Client

    def client(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    reclaim_moved_repositories((settings or _settings()).registry, previous_tag)


def test_a_moved_tag_reclaims_both_of_its_old_repositories(monkeypatch):
    # Nothing addresses them once the tag moves: cleanup on delete derives the
    # CURRENT layout, so without this the pair leaks permanently.
    quay = _Quay()

    _reclaim(monkeypatch, quay, "registry.internal/payments/hello:main")

    assert sorted(quay.deleted) == ["payments/hello", "payments/hello_cache"]


def test_the_old_path_comes_off_the_old_tag_not_off_config(monkeypatch):
    # The whole point: config already describes the NEW layout by the time this runs.
    quay = _Quay()

    _reclaim(
        monkeypatch,
        quay,
        "registry.internal/acme/payments/hello:main",
        _settings(organization="acme", repository="serverless/builders"),
    )

    assert sorted(quay.deleted) == ["acme/payments/hello", "acme/payments/hello_cache"]


def test_a_digest_reference_reclaims_the_same_pair(monkeypatch):
    quay = _Quay()

    _reclaim(monkeypatch, quay, "registry.internal/payments/hello@sha256:" + "a" * 64)

    assert sorted(quay.deleted) == ["payments/hello", "payments/hello_cache"]


def test_a_tag_on_another_registry_is_never_touched(monkeypatch, caplog):
    # This API and this token address one registry; a same-named path on another
    # host is somebody else's repository.
    quay = _Quay()

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _reclaim(monkeypatch, quay, "other.registry/payments/hello:main")

    assert quay.deleted == []
    assert any("not reclaiming" in r.message for r in _records(caplog))


def test_reclaiming_is_skipped_without_a_token(monkeypatch):
    quay = _Quay()

    _reclaim(monkeypatch, quay, "registry.internal/payments/hello:main", _settings(api_token=""))

    assert quay.deleted == []


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("registry.internal/payments/hello:main", ["payments/hello", "payments/hello_cache"]),
        (
            "registry.internal/acme/serverless/builders/payments/hello:main",
            [
                "acme/serverless/builders/payments/hello",
                "acme/serverless/builders/payments/hello_cache",
            ],
        ),
        ("registry.internal/payments/hello", ["payments/hello", "payments/hello_cache"]),
        ("elsewhere.internal/payments/hello:main", []),
    ],
)
def test_moved_repositories_derivation(tag, expected):
    assert moved_repositories(_settings().registry, tag) == expected
