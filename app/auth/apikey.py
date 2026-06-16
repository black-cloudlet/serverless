"""Static API-key authentication for admin/service automation (non-OIDC).

Keys are presented as a normal ``Authorization: Bearer <key>`` (opaque, not a
JWT) and matched against the sha256 hashes in config (constant-time). A match
yields an **admin** Principal — API keys are intended for admin/operator usage;
regular users authenticate via OIDC.
"""

from __future__ import annotations

import hashlib
import hmac

from app.auth.claims import Principal
from app.core.config import Settings


def authenticate_api_key(presented: str, settings: Settings) -> Principal | None:
    """Return an admin Principal for a valid key, or None if no key matches."""
    digest = hashlib.sha256(presented.encode()).hexdigest()
    for key in settings.api_keys:
        if hmac.compare_digest(digest, key.sha256):
            return Principal(
                subject=key.name,
                username=key.name,
                groups=key.groups,
                is_admin=True,  # API keys are admin-only
            )
    return None
