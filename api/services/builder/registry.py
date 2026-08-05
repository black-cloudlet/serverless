"""Registry cleanup: deleting the repositories a deleted function leaves behind."""

from __future__ import annotations

import httpx

from common.config import RegistryConfig
from common.logging import get_logger
from common.names import cache_repository, image_repository

logger = get_logger(__name__)


def delete_function_repositories(registry: RegistryConfig, group: str, name: str) -> None:
    """Delete a function's image and cache repositories."""
    if not registry.can_delete:
        return
    # The path the image reference hangs off, minus the host, so what is deleted
    # is what was pushed to.
    prefix = f"{registry.path}/" if registry.path else ""
    headers = {"Authorization": f"Bearer {registry.api_token}"}
    try:
        with httpx.Client(base_url=registry.api_url, timeout=registry.timeout) as client:
            for repo in (image_repository(group, name), cache_repository(group, name)):
                _delete_repository(client, headers, f"{prefix}{repo}")
    except Exception:  # noqa: BLE001 - a leftover repository is logged, not fatal
        logger.exception("registry cleanup failed for '%s' in group '%s'", name, group)


def _delete_repository(client: httpx.Client, headers: dict, repo: str) -> None:
    """Delete one repository."""
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
