"""Map OIDC claims to a Principal and the platform's group-based policy."""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.core.config import SSOConfig

# OIDC groups may arrive with a Keycloak path prefix ("/team-a") and/or an
# org-specific "ggd-<up to 4 digits>" prefix (e.g. "ggd-1234-team-a"); both are
# stripped so the bare group name is used for policy.
_GGD_PREFIX = re.compile(r"^ggd-\d{1,4}-?")


def _normalize_group(group: str) -> str:
    """Strip the Keycloak "/" path prefix and any leading ``ggd-<digits>``."""
    return _GGD_PREFIX.sub("", group.lstrip("/"))


class Principal(BaseModel):
    """The authenticated caller."""

    subject: str
    username: str
    groups: list[str] = []
    is_admin: bool = False

    def can_access_group(self, group: str) -> bool:
        """Whether the principal may act for ``group``.

        Args:
            group: The group to check.

        Returns:
            True if the principal is an admin or a member of ``group``.
        """
        return self.is_admin or group in self.groups

    @property
    def primary_group(self) -> str | None:
        """The default group (first group) used when a request omits one.

        Returns:
            The caller's first group, or None if they have none.
        """
        return self.groups[0] if self.groups else None


def principal_from_claims(claims: dict, config: SSOConfig) -> Principal:
    """Build a Principal from validated OIDC token claims.

    Normalizes group names (strips the Keycloak "/" and ``ggd-<digits>`` prefixes)
    and marks the caller an admin if any group is in the configured admin groups.

    Args:
        claims: The validated JWT claims.
        config: The SSO configuration (groups claim, admin groups).

    Returns:
        The resolved :class:`Principal`.
    """
    groups = claims.get(config.groups_claim, []) or []
    if isinstance(groups, str):
        groups = [groups]
    groups = [_normalize_group(g) for g in groups]
    is_admin = any(g in config.admin_groups for g in groups)
    return Principal(
        subject=claims.get("sub", ""),
        username=claims.get("preferred_username") or claims.get("sub", ""),
        groups=groups,
        is_admin=is_admin,
    )
