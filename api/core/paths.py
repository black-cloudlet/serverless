"""Translating between the path this app routes on and the one a client sends.

The API serves ``/v1/...``. Behind the portal's edge it is *reached* at
``/api/serverless/v1/...``, and the edge strips those two segments before the
request arrives (docs/PORTAL-INTEGRATION.md - The scheme). So there are two
paths for the same endpoint, and every place that either hands a client a path
or is handed one has to know which of the two it is holding.

Two functions, in one module, because the failure mode of doing this inline is
that one caller forgets and the bug it produces is invisible in every test that
does not run the app behind a prefix - a ``statusUrl`` that 404s only in
production, or a stream ticket signed over a path that never matches.

With no prefix configured (``external_base_path=""``, the default and what a
deployment on its own host uses) both functions are the identity, which is why
they are safe to apply unconditionally.
"""

from __future__ import annotations

from api.core.config import get_settings


def to_external(path: str) -> str:
    """The path a client outside the edge must call to reach ``path``.

    Args:
        path: An app-internal path, e.g. ``/v1/groups/payments/functions/orders``.

    Returns:
        The same path with the mount prefix in front, e.g.
        ``/api/serverless/v1/groups/payments/functions/orders``.
    """
    return f"{get_settings().external_base_path}{path}"


def to_internal(path: str) -> str:
    """The path this app routes on, from whatever a client sent.

    Tolerates both shapes on purpose. An edge that strips the prefix is the
    deployment this is written for, but Starlette routes the *unstripped* path
    too (it matches with ``root_path`` removed), so an edge that only forwards -
    or a caller that addresses the pod directly with the external path - reaches
    the same endpoint. If the two shapes then normalized differently, tickets
    would verify under one and not the other, which is a failure that depends on
    the edge's configuration rather than on anything in the request.

    Args:
        path: A path as the client wrote it, with or without the mount prefix.

    Returns:
        The path with the prefix removed if it carried one.
    """
    base = get_settings().external_base_path
    # The trailing slash matters: "/api/serverlessish/..." starts with
    # "/api/serverless" as a string and is not under it as a path.
    if base and path.startswith(f"{base}/"):
        return path[len(base) :]
    return path
