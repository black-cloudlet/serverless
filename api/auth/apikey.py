"""Static admin API-key auth (opaque ``Authorization: Bearer``)."""

from __future__ import annotations

import hmac


def verify_admin_key(token: str, admin_api_key: str) -> bool:
    """Constant-time check that the bearer ``token`` matches the admin API key.

    An empty configured key disables key auth entirely (returns ``False`` for
    every token).
    """
    if not token or not admin_api_key:
        return False
    return hmac.compare_digest(token, admin_api_key)
