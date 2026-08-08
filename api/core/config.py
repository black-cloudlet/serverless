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
    RegistryConfig,
    SiteConfig,
)


class SSOSettings(SSOConfig):
    """The shared SSO model with this deployment's defaults filled in.

    ``SSOConfig.issuer`` is required - a package shared by every API must not
    carry one environment's identity provider as a fallback - so ours is
    re-declared here, where it is ours to be wrong about.

    A subclass rather than a ``default_factory``: pydantic-settings builds the
    nested model from the env vars it finds, so a factory's defaults are lost the
    moment any sibling field is set from the environment.
    """

    issuer: str = "https://sso.internal/realms/serverless"
    # Public Keycloak client Swagger UI uses for its "Authorize" login
    # (Authorization Code + PKCE; no secret). From Helm values, not a secret.
    swagger_client_id: str = "serverless-api-swagger"


class StreamConfig(BaseModel):
    """Bounds on the Server-Sent Events streams (``/logs/stream``, ``/stats/stream``).

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
            retry.
        max_pods: Pods a single logs stream will follow. A workload scaled wide
            would otherwise let one client take the whole pool; the pods past
            the cap are named in a ``warning`` event, never dropped silently.
        interval_seconds: How often a stats stream re-reads, and how often a
            logs stream re-lists pods to pick up a scale-up or a new revision.
        min_interval_seconds: Floor for a client-supplied ``interval``.
        max_interval_seconds: Ceiling for a client-supplied ``interval``.
        heartbeat_seconds: How long a stream may produce nothing before a
            comment is sent to keep the connection from being reaped. Must stay
            well under the Route's timeout (charts - ``api.route.timeout``).
        queue_size: Lines buffered between the follower threads and the client.
            Bounded on purpose: a workload logging faster than the client reads
            must cost a reported gap, not the process's memory.
        max_seconds: Hard lifetime of one stream. Reconnecting is cheap (SSE
            does it on its own), and an immortal stream is how a leak survives
            a client that never closes its side.
        ticket_ttl_seconds: How long a stream ticket stays valid. Only ever
            spent opening one connection, so this is a window, not a session.
    """

    max_concurrent: int = Field(8, ge=1)
    max_pods: int = Field(5, ge=1)
    interval_seconds: float = Field(5.0, gt=0)
    min_interval_seconds: float = Field(1.0, gt=0)
    max_interval_seconds: float = Field(60.0, gt=0)
    heartbeat_seconds: float = Field(15.0, gt=0)
    queue_size: int = Field(1000, ge=1)
    max_seconds: int = Field(3600, ge=1)
    ticket_ttl_seconds: int = Field(60, ge=1)

    @property
    def max_workers(self) -> int:
        """Threads the stream pool is built with.

        Derived, not configured: it is exactly what the admission bounds above
        can ask for - every allowed stream following its full pod quota, plus
        one thread each for the periodic re-list or the stats fan-out. Sizing it
        by hand is how a pool ends up smaller than the admissions it has to
        serve, which turns a bound into a deadlock.
        """
        return self.max_concurrent * (self.max_pods + 1)

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

    sso: SSOSettings = Field(default_factory=SSOSettings)
    # Raw admin key from Vault via ESO. Empty (the default) disables key auth
    # rather than shipping a usable default credential.
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
