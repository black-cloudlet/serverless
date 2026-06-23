"""Static admin API-key auth (opaque ``Authorization: Bearer``).

For admin automation that cannot do OIDC, the API accepts a static key in the
same ``Authorization: Bearer`` header (docs §6). The key is supplied to the API
as the **raw token** (from Vault via ESO, env ``SERVERLESS_ADMIN_API_KEY``); the
caller sends that same raw token in the header.

The API never compares the raw strings directly. It hashes both the incoming
header token and the configured key with SHA-256 and compares the digests in
**constant time** (``hmac.compare_digest``), so verification doesn't leak the
key through timing, and an opaque token of any length is normalised to a fixed
width before comparison.
"""

from __future__ import annotations

import hashlib
import hmac


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_admin_key(token: str, admin_api_key: str) -> bool:
    """Constant-time check that the opaque bearer ``token`` matches the configured
    admin API key.

    Both sides are SHA-256 hashed before comparison. An empty configured key
    disables key auth entirely (returns ``False`` for every token).
    """
    if not token or not admin_api_key:
        return False
    return hmac.compare_digest(_sha256(token), _sha256(admin_api_key))
