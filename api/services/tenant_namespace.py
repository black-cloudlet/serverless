"""A group's namespace: asking the tenant controller to provision it.

The API cannot create namespaces, so before it writes a workload it asks the
component that can (docs/DEPLOYING.md - RBAC). "Provisioned" means *exists and
converged to the current template set*, in every region, so a workload never
deploys into a namespace still carrying last release's policies.

Nothing here fans out: it makes one call to one Service, and the controller is
what reaches the clusters.

This is a pre-flight check, and a check that could not be run has not passed:
an unreachable tenant controller is a 503, not a create that proceeds. A
controller that answered and refused is a configuration mismatch between the
two ends, reported as such.
"""

from __future__ import annotations

import asyncio

import httpx
from cloudlet_apis.logging import get_logger

from common.config import PROVISION_READY, TenantNamespaceConfig
from common.errors import ProvisioningRejectedError, ServiceUnavailableError

logger = get_logger(__name__)

# One client for the process: connection pooling across creates, and the CA
# bundle is read and parsed once instead of on every call (building an SSL
# context is blocking file I/O, and this code runs on the event loop). Built
# from the first call's settings - which are process-constant - under a lock,
# so a burst of first calls cannot each build (and leak) a pool.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _shared_client(config: TenantNamespaceConfig, verify: str | bool) -> httpx.AsyncClient:
    """The process-wide client, built on first use from the stable settings."""
    global _client  # noqa: PLW0603 - a deliberate process-wide singleton
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=config.timeout, verify=verify)
    return _client


async def close_client() -> None:
    """Close the process-wide client, at shutdown.

    Idempotent; a later call rebuilds it, since the client is created on demand.
    """
    global _client  # noqa: PLW0603 - the singleton this module owns
    async with _client_lock:
        client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def provision_namespace(
    group: str, config: TenantNamespaceConfig, verify: str | bool = True
) -> None:
    """Provision ``group``'s namespace - exists and converged, in every region.

    Args:
        group: The owning (normalized) group.
        config: The tenant-namespace settings; an empty ``controller_url``
            skips the call entirely.
        verify: The CA bundle to trust, as httpx takes it.

    Raises:
        ProvisioningRejectedError: If the controller answered with a 4xx. The
            group already passed this API's own checks, so a refusal means the
            two ends disagree (suffix, token) - an operator problem a retry
            cannot fix, and saying "retry shortly" would send them hunting an
            outage instead of a config diff.
        ServiceUnavailableError: If the controller could not be reached, or
            answered that some region did not converge. Fails closed: the
            deploy is refused rather than landing somewhere unprepared.
    """
    if not config.controller_url:
        # No tenant controller configured - a dev cluster, where the namespace
        # is whatever the operator made by hand. The skip is logged.
        logger.debug("no tenant controller configured; skipping provision for group '%s'", group)
        return

    url = f"{config.controller_url.rstrip('/')}/groups/{group}/namespace"
    headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
    try:
        client = await _shared_client(config, verify)
    except Exception as exc:  # noqa: BLE001 - a CA bundle that is missing or unreadable
        # Configuration, not an outage, so the message names what to check
        # rather than telling the caller to retry.
        logger.error("could not build the tenant-controller client (verify=%r): %s", verify, exc)
        raise ServiceUnavailableError(
            f"the client for the tenant controller could not be built ({exc}); "
            "check the CA bundle mount"
        ) from exc

    try:
        response = await client.put(url, headers=headers)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            detail = _detail_of(exc.response)
            logger.error(
                "the tenant controller rejected provisioning for group '%s' (HTTP %d): %s",
                group,
                exc.response.status_code,
                detail,
            )
            raise ProvisioningRejectedError(
                f"the tenant controller rejected group '{group}' "
                f"(HTTP {exc.response.status_code}): {detail}; the API and the "
                "controller must share one tenantNamespaces configuration"
            ) from exc
        raise _unavailable(group, exc) from exc
    except Exception as exc:  # noqa: BLE001 - network/timeout: same retryable verdict
        raise _unavailable(group, exc) from exc

    rows = body.get("regions") or []
    unconverged = [row.get("region", "?") for row in rows if row.get("status") != PROVISION_READY]
    if not rows:
        # A 200 naming no region is an answer this code does not understand,
        # and what could not be confirmed has not passed: it fails closed like
        # an unreachable controller.
        raise ServiceUnavailableError(
            f"the tenant controller gave no per-region answer for group '{group}'"
        )
    if unconverged:
        # The controller reports per region and a deploy writes to every one
        # of them, so anything short of every region ready fails the call.
        raise ServiceUnavailableError(
            f"the namespace for group '{group}' is not ready in "
            f"region(s): {', '.join(sorted(unconverged))}"
        )
    logger.info(
        "namespace '%s' provisioned for group '%s' at template set %s",
        body.get("namespace"),
        group,
        body.get("templateHash"),
    )


def _unavailable(group: str, exc: Exception) -> ServiceUnavailableError:
    """The fail-closed verdict for a controller that could not answer."""
    logger.warning("provisioning the namespace for group '%s' failed: %s", group, exc)
    return ServiceUnavailableError(
        f"could not prepare the namespace for group '{group}'; retry shortly"
    )


def _detail_of(response: httpx.Response) -> str:
    """The error body's human line, or the raw text when it is not ours."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return response.text[:200]
