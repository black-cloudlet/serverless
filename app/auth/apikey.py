"""Static admin API-key auth (opaque ``Authorization: Bearer``).

For admin automation that cannot do OIDC, the API accepts a static key in the
same ``Authorization: Bearer`` header (docs §6). The key is supplied to the API
as the **raw token** (from Vault via ESO, env ``SERVERLESS_ADMIN_API_KEY``); the
caller sends that same raw token in the header.

The incoming token is matched against the configured key with
``hmac.compare_digest``, a **constant-time** comparison, so verification doesn't
leak how many leading characters matched through timing.
"""

from __future__ import annotations

import hmac


def verify_admin_key(token: str, admin_api_key: str) -> bool:
    """Constant-time check that the opaque bearer ``token`` matches the configured
    admin API key. An empty configured key disables key auth entirely (returns
    ``False`` for every token)."""
    if not token or not admin_api_key:
        return False
    return hmac.compare_digest(token, admin_api_key)
