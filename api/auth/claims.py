"""Map OIDC claims to a Principal and the platform's group-based policy."""

from __future__ import annotations

from pydantic import BaseModel

from api.core.config import SSOConfig
from api.models.common import normalize_group


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


def principal_from_claims(claims: dict, config: SSOConfig) -> Principal:
    """Build a Principal from validated OIDC token claims.

    Normalizes group names (strips the Keycloak "/" and ``ggd-<digits>`` prefixes,
    folds "_" to "-") and marks the caller an admin if any group is in the
    configured admin groups. Request-supplied groups go through the same
    normalization, so membership checks compare canonical forms on both sides.

    Args:
        claims: The validated JWT claims.
        config: The SSO configuration (groups claim, admin groups).

    Returns:
        The resolved :class:`Principal`.
    """
    groups = claims.get(config.groups_claim, []) or []
    if isinstance(groups, str):
        groups = [groups]
    groups = [normalize_group(g) for g in groups]
    # The configured admin groups are normalized too, so an admin group written in
    # its raw SSO form ("platform_admins") still matches a normalized token group.
    admin_groups = {normalize_group(g) for g in config.admin_groups}
    is_admin = any(g in admin_groups for g in groups)
    return Principal(
        subject=claims.get("sub", ""),
        username=claims.get("preferred_username") or claims.get("sub", ""),
        groups=groups,
        is_admin=is_admin,
    )
