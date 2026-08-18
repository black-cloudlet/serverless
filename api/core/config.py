"""API service settings.

Environment-driven; in production the values come from Vault via the External
Secrets Operator. The connection identity is shared and lives in
:mod:`common.config`, the SSO model in :mod:`cloudlet_apis.auth`; this module
adds the API's own fields and this deployment's defaults for both.
"""

from __future__ import annotations

from functools import lru_cache

# The SSO model is shared - every API on the platform validates tokens the same
# way - so it lives in cloudlet_apis and is re-exported here for existing importers.
from cloudlet_apis.auth import SSOConfig  # noqa: F401
from pydantic import BaseModel, Field, model_validator

# Shared connection settings + sub-configs; re-exported for existing importers.
from common.config import (  # noqa: F401
    CABundleConfig,
    CommonSettings,
    RegionConfig,
    RegistryConfig,
)


class StreamConfig(BaseModel):
    """Bounds on the SSE streams (``/pods``, ``/logs/pods/{pod}``, ``/stats/stream``).

    A held-open stream is not a request that ends, so none of these are
    performance tuning - they are what stops streaming from consuming the
    process. A followed pod log holds a worker thread for as long as the client
    stays connected, so the threads are drawn from a pool of this module's own
    (:class:`~api.services.streams.capacity.StreamCapacity`) rather than the
    default executor every ordinary request shares, and admission is capped
    before the pool can be exhausted.

    Attributes:
        max_concurrent: Open streams allowed at once, per process. A request
            beyond it is refused with 503 rather than queued: a stream that
            connects and then stalls behind others is worse than one told to
            retry. Streams are per pod, so a client watching four pods of one
            workload spends four of these.
        interval_seconds: How often a pods or stats stream re-reads.
        min_interval_seconds: Floor for a client-supplied ``interval``.
        max_interval_seconds: Ceiling for a client-supplied ``interval``.
        heartbeat_seconds: How long a stream may produce nothing before a
            comment is sent to keep the connection from being reaped. Must stay
            well under the Route's timeout (charts - ``api.route.timeout``).
        queue_size: Lines buffered between the follower threads and the client.
            Bounded on purpose: a workload logging faster than the client reads
            must cost a reported gap, not the process's memory.
        queue_max_bytes: Frame bytes that buffer may hold, whatever the line
            count still allows. The line bound alone is no bound at all for a
            pod writing without newlines - each "line" then arrives as a ~1MB
            piece, and queue_size of those is a gigabyte per stream.
        max_seconds: Hard lifetime of one stream. Reconnecting is cheap (SSE
            does it on its own), and an immortal stream is how a leak survives
            a client that never closes its side.
        ticket_ttl_seconds: How long a stream ticket stays valid. Only ever
            spent opening one connection, so this is a window, not a session.
        snapshot_tail_lines: Newest lines a ``follow=false`` log snapshot
            returns. Applied always, whatever the caller asks: the node may
            hold tens of megabytes, and an unbounded snapshot is parsed into
            one response - large enough, that is the process's memory and the
            event loop's time, which is exactly what fails the health probes.
        snapshot_max_bytes: Hard ceiling on the bytes one snapshot reads. A
            caller's ``limitBytes`` is clamped to it, and it applies when the
            caller sets none - a backstop for pathological line lengths, where
            a line cap alone bounds nothing.
    """

    max_concurrent: int = Field(32, ge=1)
    interval_seconds: float = Field(5.0, gt=0)
    min_interval_seconds: float = Field(1.0, gt=0)
    max_interval_seconds: float = Field(60.0, gt=0)
    heartbeat_seconds: float = Field(15.0, gt=0)
    queue_size: int = Field(1000, ge=1)
    queue_max_bytes: int = Field(2 * 1024 * 1024, ge=1)
    max_seconds: int = Field(3600, ge=1)
    ticket_ttl_seconds: int = Field(60, ge=1)
    snapshot_tail_lines: int = Field(2000, ge=1)
    snapshot_max_bytes: int = Field(2 * 1024 * 1024, ge=1)

    @property
    def max_workers(self) -> int:
        """Threads the stream pool is built with.

        Derived, not configured, because a pool smaller than the admissions it
        has to serve turns a bound into a deadlock - and getting that wrong by
        hand is easy.

        Two per admitted stream. A log stream holds exactly one thread for its
        whole life (the follow). A pods or stats stream holds none between ticks
        but needs one or two briefly on each, so the second per stream is what
        keeps a tick from queueing behind the log follows.
        """
        return self.max_concurrent * 2

    @model_validator(mode="after")
    def _bounds(self) -> "StreamConfig":
        """Validate that the interval bounds and the heartbeat make sense together."""
        if self.max_interval_seconds < self.min_interval_seconds:
            raise ValueError("stream max_interval_seconds must be >= min_interval_seconds")
        if not (self.min_interval_seconds <= self.interval_seconds <= self.max_interval_seconds):
            raise ValueError(
                "stream interval_seconds must be between min_interval_seconds and "
                "max_interval_seconds"
            )
        return self


class Settings(CommonSettings):
    """API settings: the shared connection settings plus the API's own fields."""

    app_name: str = "serverless-api"
    port: int = 8080
    auth_enabled: bool = True
    # Single platform wildcard domain; host = {name}-{group}.{route_domain}
    route_domain: str = "serverless.example.com"

    # Browser origins allowed to call the API (e.g. the ServiceNow portal).
    # Empty disables CORS. env: SERVERLESS_CORS_ALLOW_ORIGINS (JSON list).
    cors_allow_origins: list[str] = Field(default_factory=list)

    # Available function runtimes, mounted as a YAML file from a ConfigMap. Absent
    # in local dev/tests -> the loader falls back to built-in defaults.
    runtimes_file: str = "/etc/serverless/runtimes/runtimes.yaml"

    # env: SERVERLESS_SSO__ISSUER, SERVERLESS_SSO__SWAGGER_CLIENT_SECRET, ...
    # The chart sets the issuer explicitly and so should any deployment: unset,
    # the package's default realm is trusted rather than startup failing.
    sso: SSOConfig = Field(default_factory=SSOConfig)
    # Raw admin key from Vault via ESO. Empty (the default) disables key auth
    # rather than shipping a usable default credential. NOT in `sso`: this is the
    # non-OIDC fallback, for admin automation that cannot do SSO at all.
    admin_api_key: str = ""

    # What the SSE streams are allowed to consume. env: SERVERLESS_STREAM__*.
    stream: StreamConfig = Field(default_factory=StreamConfig)
    # HMAC key for stream tickets, from Vault via ESO. Empty (the default)
    # disables ticket minting, exactly as an empty admin key disables key auth:
    # the streams still authenticate off the Authorization header, so a CLI
    # follow works with no extra configuration and only the browser path - which
    # cannot set that header - needs this set.
    stream_ticket_key: str = ""


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
