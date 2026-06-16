"""Map OIDC claims to a Principal and the platform's group-based policy."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.config import SSOConfig


class Principal(BaseModel):
    """The authenticated caller."""

    subject: str
    username: str
    groups: list[str] = []
    is_admin: bool = False

    def can_access_group(self, group: str) -> bool:
        """Admins can access any group; others only their own."""
        return self.is_admin or group in self.groups

    @property
    def primary_group(self) -> str:
        """Default group used to stamp newly-created resources."""
        if not self.groups:
            raise ValueError("principal has no groups")
        return self.groups[0]


def principal_from_claims(claims: dict, config: SSOConfig) -> Principal:
    groups = claims.get(config.groups_claim, []) or []
    if isinstance(groups, str):
        groups = [groups]
    # Keycloak often prefixes group paths with "/".
    groups = [g.lstrip("/") for g in groups]
    is_admin = any(g in config.admin_groups for g in groups)
    return Principal(
        subject=claims.get("sub", ""),
        username=claims.get("preferred_username") or claims.get("sub", ""),
        groups=groups,
        is_admin=is_admin,
    )
