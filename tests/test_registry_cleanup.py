"""Registry cleanup: the repositories a deleted function leaves behind."""

from __future__ import annotations

import httpx
import pytest

from api.services.registry import delete_function_repositories
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
        if self._status in (200, 202, 204):
            self.deleted.append(repo)
        return httpx.Response(self._status)


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
