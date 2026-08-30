"""urllib3 plumbing for the cluster connection pools.

What every pool carries regardless of which resource is being talked about:
TCP keepalive on the sockets, and a default connect timeout on the requests.
Both exist for the calls that carry no per-request timeout - discovery and the
build controller's watch - and for the streams that deliberately never will.
"""

from __future__ import annotations

import socket

import urllib3
from kubernetes import client
from urllib3.connection import HTTPConnection


def _keepalive_socket_options() -> list[tuple]:
    """TCP keepalive options for the cluster connection pools.

    The long-lived streams (a watch, a log follow) deliberately carry no read
    timeout - they are idle between bytes by design - which leaves them with no
    defence against a connection that dies *silently* (a NAT/conntrack entry or
    LB dropping it without RST): the server-side timeout can never arrive over
    a dead connection, and the blocked thread would sit in recv for however
    long the kernel default keepalive takes (hours). With these, the kernel
    probes an idle connection after 30s and gives up within ~a minute more, so
    a wedged watch costs minutes, not a reconcile loop until restart.

    Added to urllib3's defaults, not substituted for them: those carry
    TCP_NODELAY, and dropping it re-enables Nagle on every cluster connection.

    The TCP_* constants are Linux; anything the platform lacks is skipped, and
    SO_KEEPALIVE alone still buys the kernel-default probing.
    """
    options = [*HTTPConnection.default_socket_options]
    options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 6)):
        if hasattr(socket, name):
            options.append((socket.IPPROTO_TCP, getattr(socket, name), value))
    return options


def _default_connect_timeout(api_client: client.ApiClient, connect: float) -> None:
    """Give every request through ``api_client`` a connect timeout of its own.

    Not ``connection_pool_kw["timeout"]``: urllib3 consults the pool default
    only for its own sentinel, and ``kubernetes.client.rest`` always passes
    ``timeout=`` explicitly - ``None`` for a call with no ``_request_timeout``,
    which resolves to *no* timeout. Discovery and the watch are those calls.

    Connect only: the long-lived streams are idle between bytes by design.

    Args:
        api_client: The client whose requests should carry the default.
        connect: The connect timeout, in seconds.
    """
    pool_manager = api_client.rest_client.pool_manager
    request = pool_manager.request
    default = urllib3.Timeout(connect=connect, read=None)

    def request_with_default_connect_timeout(*args, timeout=None, **kwargs):
        return request(*args, timeout=default if timeout is None else timeout, **kwargs)

    pool_manager.request = request_with_default_connect_timeout
