"""Registry cleanup: deleting repositories nothing in the cluster owns.

A function's images and its build cache (docs/BUILDING.md - Build cache)
outlive the KSVC that produced them. Two events reclaim them: the function
being deleted, and its tag moving to a new repository, which leaves the old one
behind with nothing to ever address it again
(docs/BUILDING.md - Moving a function's repository).

Quay's management API, ``DELETE /api/v1/repository/{namespace}/{repository}``,
which removes the repository itself rather than only its manifests. That is a
Quay-specific call and needs a Quay OAuth token: robot accounts are registry
credentials and cannot authenticate here (docs/BUILDING.md - Registry cleanup on delete).
"""

from __future__ import annotations

import httpx

from common.config import RegistryConfig
from common.logging import get_logger
from common.names import CACHE_SUFFIX, cache_repository, image_repository, repository_of

logger = get_logger(__name__)


def delete_function_repositories(registry: RegistryConfig, group: str, name: str) -> None:
    """Delete a function's image and cache repositories.

    Best-effort and never raises: the workload is already gone platform-wide by
    the time this runs, and failing a delete over leftover registry content
    would report a function as undeleted when it is not.

    Args:
        registry: Registry settings, carrying the host and the API token.
        group: The owning group.
        name: The workload name.
    """
    # The same path the image reference hangs off, minus the host - so the
    # repository that is deleted is exactly the one that was pushed to.
    prefix = f"{registry.path}/" if registry.path else ""
    delete_repositories(
        registry,
        [
            f"{prefix}{repo}"
            for repo in (image_repository(group, name), cache_repository(group, name))
        ],
        subject=f"'{name}' in group '{group}'",
    )


def reclaim_moved_repositories(registry: RegistryConfig, previous_tag: str) -> None:
    """Delete the repositories a function pushed to before its tag moved.

    Nothing addresses them once the tag changes - cleanup on delete derives the
    *current* layout - so without this a layout change leaks a repository and its
    cache per function, permanently.

    Args:
        registry: Registry settings, carrying the host and the API token.
        previous_tag: The image reference the function was built at until now.
    """
    repos = moved_repositories(registry, previous_tag)
    if repos:
        delete_repositories(registry, repos, subject=f"the repository '{previous_tag}' left behind")


def moved_repositories(registry: RegistryConfig, previous_tag: str) -> list[str]:
    """The host-relative image and cache repositories ``previous_tag`` pushed to.

    Empty when the reference is on a different host: this API and this token
    address one registry, and deleting a path on another one would either 404 or,
    worse, hit a same-named repository there.

    Args:
        registry: Registry settings, carrying the host.
        previous_tag: The image reference to derive from.

    Returns:
        The two repository paths, or an empty list.
    """
    host = registry.url.strip("/")
    repository = repository_of(previous_tag)
    if not repository.startswith(f"{host}/"):
        logger.info("not reclaiming '%s': it is not on %s", previous_tag, host)
        return []
    path = repository[len(host) + 1 :]
    return [path, f"{path}{CACHE_SUFFIX}"]


def delete_repositories(registry: RegistryConfig, repos: list[str], *, subject: str) -> None:
    """Delete repository paths, best-effort and never raising.

    Args:
        registry: Registry settings, carrying the host and the API token.
        repos: Host-relative ``{namespace}/{repository}`` paths.
        subject: What is being reclaimed, for the log line on failure.
    """
    if not registry.can_delete:
        return
    headers = {"Authorization": f"Bearer {registry.api_token}"}
    try:
        with httpx.Client(base_url=registry.api_url, timeout=registry.timeout) as client:
            for repo in repos:
                _delete_repository(client, headers, repo)
    except Exception:  # noqa: BLE001 - a leftover repository is logged, not fatal
        logger.exception("registry cleanup failed for %s", subject)


def _delete_repository(client: httpx.Client, headers: dict, repo: str) -> None:
    """Delete one repository.

    The outcomes are read off httpx rather than compared against literals:
    ``is_success`` is the whole 2xx class, which is what "deleted" means here.
    Quay answers a repository delete with 204, but listing the codes we happen
    to have seen would quietly log a successful 200 as a failure.

    Args:
        client: The registry HTTP client.
        headers: Authorization headers carrying the OAuth token.
        repo: The ``{namespace}/{repository}`` path the Quay route expects.
    """
    resp = client.delete(f"/api/v1/repository/{repo}", headers=headers)
    if resp.is_success:
        logger.info("deleted registry repository '%s'", repo)
    elif resp.status_code == httpx.codes.NOT_FOUND:
        pass  # never pushed, or already gone
    elif resp.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        # The token acts as the user who authorized it: that user needs admin on
        # this namespace, and with no `registry.organization` every group is one.
        logger.warning(
            "not authorized to delete registry repository '%s' (%s); "
            "the API token needs admin on that namespace",
            repo,
            resp.status_code,
        )
    else:
        logger.warning("could not delete registry repository '%s': %s", repo, resp.status_code)
