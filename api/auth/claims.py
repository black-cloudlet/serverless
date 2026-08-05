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
        """Whether the principal may act for ``group``."""
        return self.is_admin or group in self.groups


def principal_from_claims(claims: dict, config: SSOConfig) -> Principal:
    """Build a Principal from validated OIDC token claims."""
    groups = claims.get(config.groups_claim, []) or []
    if isinstance(groups, str):
        groups = [groups]
    groups = [normalize_group(g) for g in groups]
    admin_groups = {normalize_group(g) for g in config.admin_groups}
    is_admin = any(g in admin_groups for g in groups)
    return Principal(
        subject=claims.get("sub", ""),
        username=claims.get("preferred_username") or claims.get("sub", ""),
        groups=groups,
        is_admin=is_admin,
    )
