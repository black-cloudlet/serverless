"""API service settings.

Environment-driven; in production the values come from Vault via the External
Secrets Operator. The connection identity is shared and lives in
:mod:`common.config`, the SSO model in :mod:`cloudlet_apis.auth`; this module
adds the API's own fields and this deployment's defaults for both.
"""

from __future__ import annotations

from functools import lru_cache

# The SSO model lives in cloudlet_apis, shared with every API on the platform;
# re-exported here so importers can take it from this module.
from cloudlet_apis.auth import SSOConfig  # noqa: F401
from pydantic import BaseModel, Field, field_validator, model_validator

# Shared connection settings + sub-configs, re-exported from this module.
from common.config import (  # noqa: F401
    CABundleConfig,
    CommonSettings,
    RegionConfig,
    RegistryConfig,
)


class StreamConfig(BaseModel):
    """Bounds on the SSE streams (``/pods``, ``/logs/pods/{pod}``, ``/stats/stream``).

    A followed pod log holds a worker thread for as long as the client stays
    connected, so stream threads are drawn from
    :class:`~api.services.streams.capacity.StreamCapacity`'s own pool, not the
    default executor, and admission is capped before that pool can be exhausted
    (docs/STREAMING.md - A held-open stream holds a thread).

    Attributes:
        max_concurrent: Open streams allowed at once, per process. A request
            beyond it is refused with 503, not queued. Streams are per pod, so
            a client watching four pods of one workload spends four of these.
        interval_seconds: How often a pods or stats stream re-reads.
        min_interval_seconds: Floor for a client-supplied ``interval``.
        max_interval_seconds: Ceiling for a client-supplied ``interval``.
        heartbeat_seconds: How long a stream may produce nothing before a
            comment is sent to keep the connection from being reaped. Must stay
            well under the Route's timeout (charts - ``api.route.timeout``).
        queue_size: Lines buffered between the follower threads and the client.
            Past it, lines are dropped and the gap is reported to the client.
        queue_max_bytes: Frame bytes that buffer may hold, whatever the line
            count still allows; it bounds a pod writing without newlines, whose
            "lines" the line count alone does not bound.
        max_seconds: Hard lifetime of one stream, after which it ends itself
            with an ``end`` event and the client reconnects.
        ticket_ttl_seconds: How long a stream ticket stays valid. A ticket is
            spent opening one connection.
        snapshot_tail_lines: Newest lines a ``follow=false`` log snapshot
            returns, applied always, whatever the caller asks
            (docs/STREAMING.md - ``follow=false``).
        snapshot_max_bytes: Hard ceiling on the bytes one snapshot reads. A
            caller's ``limitBytes`` is clamped to it, and it applies when the
            caller sets none.
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
        """Threads the stream pool is built with, derived from the admission cap.

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

    # The path the whole API is served under - endpoints, docs, OpenAPI, the
    # SSO token proxy. The chart ships /api/serverless; empty (this default)
    # serves /v1/... bare.
    # env: SERVERLESS_BASE_PATH.
    base_path: str = ""

    # Browser origins allowed to call the API (e.g. the ServiceNow portal).
    # Empty disables CORS. env: SERVERLESS_CORS_ALLOW_ORIGINS (JSON list).
    cors_allow_origins: list[str] = Field(default_factory=list)

    # Available function runtimes, mounted as a YAML file from a ConfigMap. Absent
    # in local dev/tests -> the loader falls back to built-in defaults.
    runtimes_file: str = "/etc/serverless/runtimes/runtimes.yaml"

    # env: SERVERLESS_SSO__ISSUER, SERVERLESS_SSO__SWAGGER_CLIENT_SECRET, ...
    # The chart sets the issuer; unset, the package's default realm is trusted.
    sso: SSOConfig = Field(default_factory=SSOConfig)
    # Raw admin key from Vault via ESO. Empty (the default) disables key auth.
    # Separate from `sso`: the non-OIDC fallback, for admin automation that
    # cannot do SSO at all (docs/API.md - Static API keys).
    admin_api_key: str = ""

    # What the SSE streams are allowed to consume. env: SERVERLESS_STREAM__*.
    stream: StreamConfig = Field(default_factory=StreamConfig)
    # HMAC key for stream tickets, from Vault via ESO. Empty (the default)
    # disables ticket minting; the streams still authenticate off the
    # Authorization header, so only the browser path - which cannot set that
    # header - depends on this (docs/STREAMING.md - Browsers cannot send an
    # `Authorization` header).
    stream_ticket_key: str = ""

    @field_validator("base_path")
    @classmethod
    def _normalize_base_path(cls, value: str) -> str:
        """Accept the base path in the one shape the rest of the code assumes.

        Every path the API serves is concatenated onto this value.

        Args:
            value: The configured base path.

        Returns:
            Either empty, or a path with a leading and no trailing slash.

        Raises:
            ValueError: If a non-empty base path has no leading slash.
        """
        value = value.rstrip("/")
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError(f"base_path must start with '/' (got {value!r})")
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
