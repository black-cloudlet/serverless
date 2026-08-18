"""The API's own auth wiring: the bearer admin key next to SSO."""

import pytest

from api.core.config import Settings, SSOConfig


def _auth_with_admin_key(monkeypatch, raw_key, admin_groups=("platform-admins",)):
    """Point api.auth.deps at settings carrying ``raw_key``, as production does.

    Goes through ``get_auth`` rather than building an ``SSOAuth`` here, so what
    is under test is this API's wiring - which settings the shared component is
    built from - and not a second copy of it that could quietly drift.
    """
    from api.auth.deps import get_auth

    settings = Settings(
        auth_enabled=True,
        admin_api_key=raw_key,
        sso=SSOConfig(admin_groups=list(admin_groups)),
    )
    monkeypatch.setattr("api.auth.deps.get_settings", lambda: settings)
    get_auth.cache_clear()


def test_require_auth_via_bearer_admin_key(monkeypatch):
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "opaque-s3cret")
    # Opaque admin key in the standard Authorization: Bearer header.
    req = SimpleNamespace(headers={"Authorization": "Bearer opaque-s3cret"})
    p = require_auth(req)
    assert p.username == "admin" and p.is_admin is True
    # The configured admin groups, paired the same way a token's claim is: the
    # normalized name, and the spelling config used alongside it.
    assert [(g.name, g.sso_name) for g in p.groups] == [("platform-admins", "platform-admins")]
    assert p.can_access_group("platform-admins") is True

    # An unrecognised, non-JWT token must be rejected, not silently allowed.
    bad = SimpleNamespace(headers={"Authorization": "Bearer nope"})
    with pytest.raises(UnauthenticatedError):
        require_auth(bad)


def test_admin_key_header_carries_raw_token_not_a_hash(monkeypatch):
    """The header must carry the RAW token, matched directly against the configured
    key. A sha256 hash of the key is not the key, so it must not authenticate."""
    import hashlib
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "opaque-s3cret")
    hashed = hashlib.sha256(b"opaque-s3cret").hexdigest()
    req = SimpleNamespace(headers={"Authorization": f"Bearer {hashed}"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req)


def test_empty_admin_key_disables_key_auth(monkeypatch):
    """Setting the admin key empty disables API-key auth entirely; no opaque token
    (including the empty string) is accepted."""
    from types import SimpleNamespace

    from api.auth.deps import require_auth
    from common.errors import UnauthenticatedError

    _auth_with_admin_key(monkeypatch, "")
    req = SimpleNamespace(headers={"Authorization": "Bearer anything"})
    with pytest.raises(UnauthenticatedError):
        require_auth(req)
