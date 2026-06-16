"""Static API-key authentication for non-OIDC service accounts.

Keys are presented in the ``X-API-Key`` header and matched against the
sha256 hashes in config (constant-time). A match yields a Principal whose
groups drive the same group-based authorization as OIDC users.
"""

from __future__ import annotations

import hashlib
import hmac

from app.auth.claims import Principal
from app.core.config import Settings


def authenticate_api_key(presented: str, settings: Settings) -> Principal | None:
    """Return a Principal for a valid key, or None if no key matches."""
    digest = hashlib.sha256(presented.encode()).hexdigest()
    for key in settings.api_keys:
        if hmac.compare_digest(digest, key.sha256):
            is_admin = any(g in settings.sso.admin_groups for g in key.groups)
            return Principal(
                subject=key.name,
                username=key.name,
                groups=key.groups,
                is_admin=is_admin,
            )
    return None
