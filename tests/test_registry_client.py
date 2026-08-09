"""The shared registry client: the management-API mechanics both services use.

The repository-delete path is exercised end-to-end through the API's cleanup in
``test_registry_cleanup.py``; what is pinned here is the client's own surface -
tag listing and tag deletion, which the build controller's GC consumes, and the
host-relative path derivation both callers address Quay by.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from common.config import CommonSettings
from common.registry import RegistryClient, TagInfo, repository_path

_LOGGER = "common.registry"


def _registry(**overrides):
    base = dict(url="registry.internal", api_token="oauth-token")
    base.update(overrides)
    return CommonSettings(registry=base).registry


class _Quay:
    """A management API serving a tag listing and recording tag deletes."""

    def __init__(self, pages=None, status=200, delete_status=204):
        # Each page: the `tags` list of dicts; `has_additional` is derived.
        self._pages = pages if pages is not None else [[]]
        self._status = status
        self._delete_status = delete_status
        self.deleted_tags: list[str] = []
        self.listed_pages: list[int] = []
        self.tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("Authorization", ""))
        path = request.url.path.removeprefix("/api/v1/repository/")
        if request.method == "GET":
            if self._status != 200:
                return httpx.Response(self._status)
            page = int(request.url.params.get("page", "1"))
            self.listed_pages.append(page)
            tags = self._pages[page - 1] if page <= len(self._pages) else []
            body = {"tags": tags, "has_additional": page < len(self._pages)}
            return httpx.Response(200, content=json.dumps(body))
        if request.method == "DELETE":
            resp = httpx.Response(self._delete_status)
            if resp.is_success:
                self.deleted_tags.append(path)
            return resp
        return httpx.Response(405)


def _client(monkeypatch, quay: _Quay, registry=None) -> RegistryClient:
    transport = httpx.MockTransport(quay.handler)
    real = httpx.Client

    def client(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return RegistryClient(registry or _registry())


# --------------------------------------------------------------------------- #
# listing tags                                                                 #
# --------------------------------------------------------------------------- #


def test_tags_come_back_with_the_digest_and_ordering_fields(monkeypatch):
    quay = _Quay(
        pages=[
            [
                {"name": "main", "manifest_digest": "sha256:aa", "start_ts": 300},
                {"name": "b1.20260101.100000", "manifest_digest": "sha256:aa", "start_ts": 100},
            ]
        ]
    )
    with _client(monkeypatch, quay) as client:
        tags = client.list_tags("payments/hello")
    # the digest is what "still running" is decided against, and start_ts is
    # what "the newest N" is decided on - a pruner needs both, so they are pinned
    assert tags == [
        TagInfo(name="main", digest="sha256:aa", start_ts=300),
        TagInfo(name="b1.20260101.100000", digest="sha256:aa", start_ts=100),
    ]


def test_the_listing_follows_pagination_to_the_end(monkeypatch):
    quay = _Quay(
        pages=[
            [{"name": "b2.20260102.100000", "manifest_digest": "sha256:bb", "start_ts": 200}],
            [{"name": "b1.20260101.100000", "manifest_digest": "sha256:aa", "start_ts": 100}],
        ]
    )
    with _client(monkeypatch, quay) as client:
        tags = client.list_tags("payments/hello")
    # a repository past one page must not silently truncate: a pruner that only
    # ever saw page one would judge "newest N" against a partial history
    assert quay.listed_pages == [1, 2]
    assert [t.name for t in tags] == ["b2.20260102.100000", "b1.20260101.100000"]


def test_paging_that_never_terminates_is_cut_off_and_returns_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)

    def always_more(request: httpx.Request) -> httpx.Response:
        # a proxy dropping the `page` param serves page one, with more, forever
        body = {"tags": [{"name": "main", "manifest_digest": "sha256:aa"}], "has_additional": True}
        return httpx.Response(200, content=json.dumps(body))

    quay = _Quay()
    quay.handler = always_more
    with _client(monkeypatch, quay) as client:
        tags = client.list_tags("payments/hello")

    # a partial listing is NOT returned: "newest N" judged on a partial set
    # could prune a tag that is genuinely among the newest
    assert tags == []
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert any("did not terminate" in r.getMessage() for r in records)


def test_a_missing_repository_lists_as_empty(monkeypatch):
    quay = _Quay(status=404)
    with _client(monkeypatch, quay) as client:
        # the function being deleted mid-sweep: nothing to act on, not an error
        assert client.list_tags("payments/hello") == []


def test_a_refused_listing_is_empty_and_warned_about(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay(status=500)
    with _client(monkeypatch, quay) as client:
        assert client.list_tags("payments/hello") == []
    # empty is the safe direction: a pruner deletes only what a listing
    # returned, so "no listing" is "no deletes", never "delete everything"
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "could not list tags" in records[0].getMessage()


def test_entries_without_a_name_are_skipped(monkeypatch):
    quay = _Quay(pages=[[{"manifest_digest": "sha256:aa"}, {"name": "main"}]])
    with _client(monkeypatch, quay) as client:
        tags = client.list_tags("payments/hello")
    # a nameless entry cannot be deleted or protected; absent digest/ts default
    # rather than fail the whole listing
    assert tags == [TagInfo(name="main", digest="", start_ts=0)]


# --------------------------------------------------------------------------- #
# deleting tags                                                                #
# --------------------------------------------------------------------------- #


def test_a_deleted_tag_is_confirmed(monkeypatch):
    quay = _Quay()
    with _client(monkeypatch, quay) as client:
        assert client.delete_tag("payments/hello", "b1.20260101.100000") is True
    assert quay.deleted_tags == ["payments/hello/tag/b1.20260101.100000"]


def test_a_tag_already_gone_is_not_a_success_and_not_a_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay(delete_status=404)
    with _client(monkeypatch, quay) as client:
        assert client.delete_tag("payments/hello", "b1.20260101.100000") is False
    # the outcome was arrived at by someone else; a sweep must not log it as noise
    assert [r for r in caplog.records if r.name == _LOGGER] == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_tag_delete_names_the_token(monkeypatch, caplog, status):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    quay = _Quay(delete_status=status)
    with _client(monkeypatch, quay) as client:
        assert client.delete_tag("payments/hello", "main") is False
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "not authorized" in records[0].getMessage()


def test_the_oauth_token_rides_every_request(monkeypatch):
    quay = _Quay(pages=[[{"name": "main"}]])
    with _client(monkeypatch, quay) as client:
        client.list_tags("payments/hello")
        client.delete_tag("payments/hello", "main")
    # a robot's basic credentials would not authenticate against /api/v1 at all
    assert quay.tokens == ["Bearer oauth-token"] * 2


def test_the_client_refuses_to_run_outside_its_context(monkeypatch):
    client = _client(monkeypatch, _Quay())
    with pytest.raises(RuntimeError):
        client.list_tags("payments/hello")


# --------------------------------------------------------------------------- #
# the shared path derivation                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("registry.internal/payments/hello:main", "payments/hello"),
        ("registry.internal/acme/fns/payments/hello:main", "acme/fns/payments/hello"),
        ("registry.internal/payments/hello@sha256:" + "a" * 64, "payments/hello"),
        ("registry.internal/payments/hello", "payments/hello"),
        # this API and its token address one registry; a same-named path on
        # another host is somebody else's repository
        ("elsewhere.internal/payments/hello:main", None),
    ],
)
def test_repository_path_derivation(image, expected):
    assert repository_path(_registry(), image) == expected


def test_repository_path_matches_the_host_the_references_are_built_on(monkeypatch):
    # a pasted scheme is stripped the same way image references strip it, so
    # what was pushed under this host is recognized as ours
    registry = _registry(url="https://registry.internal/")
    assert repository_path(registry, "registry.internal/payments/hello:main") == "payments/hello"
