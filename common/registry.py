"""Quay management-API mechanics, shared by the API and the build controller.

Registry content has no owner in any cluster, so reclaiming it is always an
explicit call against Quay's management API (``/api/v1``). The API deletes a
deleted function's repositories (:mod:`api.services.builder.registry`); the
build controller's pruning of the per-build tags kpack accumulates sits on the
same client. Every caller addresses one registry, authenticated with its OAuth
token - robot accounts are registry credentials for ``/v2`` and cannot call
``/api/v1`` at all (docs/BUILDING.md - Registry cleanup on delete).

Mechanism only. *What* may be deleted - which repositories, which tags must
survive - is each caller's policy and stays with the caller; what lives here is
how one registry is addressed, so the two services cannot drift in how they
talk to it. In the layering this is a domain module: httpx, no kubernetes, no
web framework (``tests/test_layering.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from cloudlet_apis.logging import get_logger

from common.config import RegistryConfig
from common.names import repository_of

logger = get_logger(__name__)

# Quay's maximum page size for the tag listing; fewer round-trips per repository.
_TAG_PAGE_LIMIT = 100
# Cap on the pages one tag listing walks (10,000 tags). Paging can fail to
# terminate - a proxy that drops the `page` param serves page one with
# `has_additional` set forever - and this loop runs on the reconcile loop's only
# thread.
_TAG_MAX_PAGES = 100


def repository_path(registry: RegistryConfig, image: str) -> str | None:
    """The host-relative repository an image reference names on ``registry``.

    ``/api/v1`` routes address a repository as ``{namespace}/{repository}`` with
    no host, so the host is checked and then removed. None when the reference
    sits on a different host: this client and its token address one registry,
    and a path derived here is never sent to another.

    Args:
        registry: Registry settings, carrying the host.
        image: An image reference; a tag and/or digest is tolerated.

    Returns:
        The ``{namespace}/{repository}`` path, or None for a foreign host.
    """
    repository = repository_of(image)
    if not repository.startswith(f"{registry.host}/"):
        return None
    return repository[len(registry.host) + 1 :]


@dataclass(frozen=True)
class TagInfo:
    """One active tag, as Quay's tag listing reports it."""

    name: str
    # What the tag currently points at (``manifest_digest``). A pruner keeps
    # every tag whose digest something still runs: deleting the last tag on a
    # manifest lets the registry collect it, and a digest-pinned revision that
    # re-pulls after that fails.
    digest: str
    # When the tag started pointing there (``start_ts``, epoch seconds) - what
    # "the newest N" is decided on.
    start_ts: int


class RegistryClient:
    """One registry's management API, spoken over one HTTP connection.

    A context manager: the connection opens on ``__enter__`` and every call on
    it shares one base URL, token and timeout, so a sweep over many repositories
    handshakes once.

    Methods turn HTTP statuses into log lines and return values instead of
    raising: a leftover repository or tag is logged, never a failed operation
    (docs/BUILDING.md - Lifecycle & Cleanup). A 401/403 is logged as the token
    lacking namespace admin, a 404 as the outcome already reached. Transport
    errors propagate to the caller.
    """

    def __init__(self, registry: RegistryConfig):
        """Hold the settings; the connection opens on ``__enter__``.

        Args:
            registry: Registry settings, carrying the host and the API token.
        """
        self._registry = registry
        self._client: httpx.Client | None = None

    def __enter__(self) -> RegistryClient:
        self._client = httpx.Client(
            base_url=self._registry.api_url,
            timeout=self._registry.timeout,
            headers={"Authorization": f"Bearer {self._registry.api_token}"},
        )
        return self

    def __exit__(self, *exc_info) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def _http(self) -> httpx.Client:
        """The open connection, or a refusal to run outside the context."""
        if self._client is None:
            raise RuntimeError("RegistryClient must be entered as a context manager")
        return self._client

    def delete_repository(self, repo: str) -> None:
        """Delete one repository outright, with everything in it.

        Args:
            repo: The ``{namespace}/{repository}`` path the Quay route expects.
        """
        resp = self._http.delete(f"/api/v1/repository/{repo}")
        self._deleted(resp, f"repository '{repo}'")

    def list_tags(self, repo: str) -> list[TagInfo]:
        """Every active tag in a repository, across however many pages Quay serves.

        Empty on a 404 (never pushed, or deleted mid-sweep) and on any other
        refusal, which is logged. A pruner deletes only what a listing returned,
        so an empty listing deletes nothing.

        Args:
            repo: The ``{namespace}/{repository}`` path the Quay route expects.

        Returns:
            The active tags, with the digest each points at.
        """
        tags: list[TagInfo] = []
        for page in range(1, _TAG_MAX_PAGES + 1):
            resp = self._http.get(
                f"/api/v1/repository/{repo}/tag/",
                params={"onlyActiveTags": "true", "limit": _TAG_PAGE_LIMIT, "page": page},
            )
            if resp.status_code == httpx.codes.NOT_FOUND:
                return []  # the repository is gone; nothing to act on
            if not resp.is_success:
                logger.warning("could not list tags of '%s': %s", repo, resp.status_code)
                return []
            body = resp.json()
            tags.extend(
                TagInfo(
                    name=tag["name"],
                    digest=tag.get("manifest_digest") or "",
                    start_ts=int(tag.get("start_ts") or 0),
                )
                for tag in body.get("tags", [])
                if tag.get("name")
            )
            if not body.get("has_additional"):
                return tags
        # Non-terminating paging. A partial listing is not returned: a "newest N"
        # judged on a partial set could prune a tag that is among the newest.
        logger.warning(
            "tag listing of '%s' did not terminate after %d pages; treating as unlistable",
            repo,
            _TAG_MAX_PAGES,
        )
        return []

    def delete_tag(self, repo: str, tag: str) -> bool:
        """Delete one tag, leaving the repository and its other tags in place.

        Quay moves the manifest into its time machine rather than freeing the
        bytes at once, so quota comes back when the expiration window passes -
        not on this call.

        Args:
            repo: The ``{namespace}/{repository}`` path the Quay route expects.
            tag: The tag name to delete.

        Returns:
            True if the registry confirmed the delete.
        """
        resp = self._http.delete(f"/api/v1/repository/{repo}/tag/{tag}")
        return self._deleted(resp, f"tag '{repo}:{tag}'")

    @staticmethod
    def _deleted(resp: httpx.Response, subject: str) -> bool:
        """Judge one delete's outcome, identically for a repository and a tag.

        The registry answers the repository and the tag route the same way, so
        both run through one ladder: any 2xx (``is_success``, not a list of the
        codes Quay happens to send) is a delete, a 404 is already gone and is not
        logged, a 401/403 is the token lacking namespace admin, and any other
        status is logged as a failure.

        Args:
            resp: The registry's answer.
            subject: What was addressed, e.g. ``repository 'ns/repo'``.

        Returns:
            True if the registry confirmed the delete.
        """
        if resp.is_success:
            logger.info("deleted registry %s", subject)
            return True
        if resp.status_code == httpx.codes.NOT_FOUND:
            return False  # never pushed, or already gone - not worth a line
        if resp.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            # The token acts as the user who authorized it: that user needs admin
            # on this namespace, and with no `registry.organization` every group is one.
            logger.warning(
                "not authorized to delete registry %s (%s); "
                "the API token needs admin on that namespace",
                subject,
                resp.status_code,
            )
            return False
        logger.warning("could not delete registry %s: %s", subject, resp.status_code)
        return False
