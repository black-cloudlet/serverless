"""Translating between the path this app routes on and the one a client sends.

The app serves ``/v1/...``. Mounted under a prefix it is reached at
``{prefix}/v1/...``, so anything the API hands a client needs the prefix put
back, and anything a client hands the API needs it taken off. Both are the
identity when no prefix is configured.
"""

from __future__ import annotations

from api.core.config import get_settings


def to_external(path: str) -> str:
    """The path a client outside the mount must call to reach ``path``.

    Args:
        path: An app-internal path.

    Returns:
        The same path with the mount prefix in front.
    """
    return f"{get_settings().external_base_path}{path}"


def to_internal(path: str) -> str:
    """The path this app routes on, from whatever a client sent.

    Accepts both shapes: an edge that strips the prefix is what this is for, but
    Starlette routes the unstripped path too, and the two must normalize alike
    or a ticket would verify under one and not the other.

    Args:
        path: A path as the client wrote it, with or without the prefix.

    Returns:
        The path with the prefix removed if it carried one.
    """
    base = get_settings().external_base_path
    # "/api/serverlessish/..." starts with "/api/serverless" and is not under it.
    if base and path.startswith(f"{base}/"):
        return path[len(base) :]
    return path
