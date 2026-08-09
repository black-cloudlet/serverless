"""Registry cleanup: deleting repositories nothing in the cluster owns.

A function's images and its build cache (docs/BUILDING.md - Build cache)
outlive the KSVC that produced them. Two events reclaim them: the function
being deleted, and its tag moving to a new repository, which leaves the old one
behind with nothing to ever address it again
(docs/BUILDING.md - Moving a function's repository).

Policy only - *which* repositories a function event reclaims, and that the
whole cleanup is best-effort. How the registry is spoken to is
:class:`common.registry.RegistryClient`, shared with the build controller so
the two services cannot drift in how they address Quay
(docs/BUILDING.md - Registry cleanup on delete).
"""

from __future__ import annotations

from cloudlet_apis.logging import get_logger

from common.config import RegistryConfig
from common.names import CACHE_SUFFIX, cache_repository, image_repository
from common.registry import RegistryClient, repository_path

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

    Empty when the reference is on a different host - the shared derivation
    (:func:`common.registry.repository_path`) refuses those, and the refusal is
    logged here because only this caller knows it means "not reclaiming".

    Args:
        registry: Registry settings, carrying the host.
        previous_tag: The image reference to derive from.

    Returns:
        The two repository paths, or an empty list.
    """
    path = repository_path(registry, previous_tag)
    if path is None:
        logger.info("not reclaiming '%s': it is not on %s", previous_tag, registry.host)
        return []
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
    try:
        with RegistryClient(registry) as client:
            for repo in repos:
                client.delete_repository(repo)
    except Exception:  # noqa: BLE001 - a leftover repository is logged, not fatal
        logger.exception("registry cleanup failed for %s", subject)
