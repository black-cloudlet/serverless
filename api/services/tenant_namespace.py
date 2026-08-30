"""A group's namespace: asking the tenant controller to make it ready.

The API cannot create namespaces - that is the whole point of the split - so
before it writes a workload it asks the component that can. "Ensured" means
*exists and converged to the current template set*, in every region, so a
workload never deploys into a namespace still carrying last release's
policies.

Not under ``regions/``, though it is about every region: nothing here fans
out. It makes one call to one Service, and the controller is what reaches the
clusters. What it is about is the tenant namespace, so that is what it is
called.

This is a pre-flight check, and it obeys the rule the rest of them do: a check
that could not be run has not passed. An unreachable tenant controller is a
503, not
a shrug - deploying into a namespace nobody has confirmed is the failure the
call exists to prevent.
"""

from __future__ import annotations

import httpx
from cloudlet_apis.logging import get_logger

from common.config import TenantNamespaceConfig
from common.errors import ServiceUnavailableError

logger = get_logger(__name__)

# The status values the controller reports per region (provisioner/ensure.py).
READY = "Ready"


async def ensure_namespace(
    group: str, config: TenantNamespaceConfig, verify: str | bool = True
) -> None:
    """Ensure ``group``'s namespace exists and is converged, in every region.

    Args:
        group: The owning (normalized) group.
        config: The tenant-namespace settings; an empty ``controller_url``
            skips the call entirely.
        verify: The CA bundle to trust, as httpx takes it.

    Raises:
        ServiceUnavailableError: If the controller could not be reached, or
            answered that some region did not converge. Fails closed: the
            create is refused rather than landing somewhere unprepared.
    """
    if not config.controller_url:
        # No tenant controller configured - a dev cluster, where the namespace is
        # whatever the operator made by hand. Skipping is a decision, so it is
        # logged rather than silent.
        logger.debug("no tenant controller configured; skipping ensure for group '%s'", group)
        return

    url = f"{config.controller_url.rstrip('/')}/groups/{group}/namespace"
    headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
    try:
        async with httpx.AsyncClient(timeout=config.timeout, verify=verify) as client:
            response = await client.put(url, headers=headers)
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001 - every failure is the same verdict
        logger.warning("ensuring namespace for group '%s' failed: %s", group, exc)
        raise ServiceUnavailableError(
            f"could not prepare the namespace for group '{group}'; retry shortly"
        ) from exc

    rows = body.get("regions") or []
    unconverged = [row.get("region", "?") for row in rows if row.get("status") != READY]
    if not rows:
        # A 200 that names no region is not a converged namespace - it is an
        # answer this code does not understand, and the rule here is that what
        # could not be confirmed has not passed. Defaulting to "no rows, so
        # nothing unconverged" would have read silence as consent.
        raise ServiceUnavailableError(
            f"the tenant controller gave no per-region answer for group '{group}'"
        )
    if unconverged:
        # A partial ensure is not a success. The tenant controller reports per region
        # and a create writes to all of them, so anything short of every region
        # ready would put a workload in a namespace that is not prepared.
        raise ServiceUnavailableError(
            f"the namespace for group '{group}' is not ready in "
            f"region(s): {', '.join(sorted(unconverged))}"
        )
    logger.info(
        "namespace '%s' ensured for group '%s' at template set %s",
        body.get("namespace"),
        group,
        body.get("templateHash"),
    )
