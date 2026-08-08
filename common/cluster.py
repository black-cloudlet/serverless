"""Per-site Kubernetes/OpenShift cluster client (server-side apply, get, watch, delete).

Shared infrastructure: the API and the build controller both reach a cluster the
same way (client-cert mTLS, lazy connect), configured from the shared
:class:`~common.config.CommonSettings`. ``ResourceKind`` is the registry of GVKs
the platform operates on.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum

from kubernetes import client, utils
from kubernetes.dynamic import DynamicClient

from common.config import CommonSettings, SiteConfig
from common.errors import NotFoundError, ValidationError


class ResourceKind(Enum):
    """The resource kinds the platform manages, mapped to (apiVersion, kind)."""

    KNATIVE_SERVICE = ("serving.knative.dev/v1", "Service")
    KNATIVE_REVISION = ("serving.knative.dev/v1", "Revision")
    DOMAIN_MAPPING = ("serving.knative.dev/v1beta1", "DomainMapping")
    CONFIG_MAP = ("v1", "ConfigMap")
    SECRET = ("v1", "Secret")
    SERVICE_ACCOUNT = ("v1", "ServiceAccount")
    POD = ("v1", "Pod")
    POD_METRICS = ("metrics.k8s.io/v1beta1", "PodMetrics")
    KPACK_IMAGE = ("kpack.io/v1alpha2", "Image")
    KPACK_BUILD = ("kpack.io/v1alpha2", "Build")

    @property
    def api_version(self) -> str:
        """The resource's ``apiVersion`` (e.g. ``serving.knative.dev/v1``)."""
        return self.value[0]

    @property
    def kind(self) -> str:
        """The resource's PascalCase ``kind`` (e.g. ``Service``)."""
        return self.value[1]

    @classmethod
    def from_kind(cls, kind: str) -> "ResourceKind":
        """Map a manifest's ``kind`` string (e.g. "Secret") to its ResourceKind.

        Args:
            kind: The PascalCase kind string.

        Returns:
            The matching ResourceKind.

        Raises:
            ValueError: If no member has that kind.
        """
        for member in cls:
            if member.kind == kind:
                return member
        raise ValueError(f"unknown resource kind: {kind!r}")


class Cluster:
    """A single site's cluster connection and resource operations.

    The Kubernetes client is synchronous and the connection is established lazily
    (on first use) so one unreachable site can't fail or block startup.
    """

    def __init__(self, site_config: SiteConfig, settings: CommonSettings):
        """Configure the client for one site (the connection stays lazy).

        Args:
            site_config: The site's name and cluster identifiers.
            settings: Shared connection settings (namespace, TLS material, base
                domain, timeouts).
        """
        self.site: str = site_config.name
        self.name: str = site_config.cluster
        self._namespace: str = settings.workloads_namespace

        self._configuration = client.Configuration()
        self._configuration.host = f"https://api.{self.name}.{settings.base_domain}:6443"

        self._configuration.ssl_ca_cert = settings.ca_bundle.file
        self._configuration.cert_file = settings.client_cert_file
        self._configuration.key_file = settings.client_key_file

        self._api_client_obj: client.ApiClient | None = None
        self._dynamic_client_obj: DynamicClient | None = None
        self._opts: dict = {
            "_request_timeout": (
                settings.cluster_connect_timeout,
                settings.cluster_read_timeout,
            )
        }

    @property
    def _api_client(self) -> client.ApiClient:
        """The lazily-built Kubernetes API client for this site."""
        if self._api_client_obj is None:
            self._api_client_obj = client.ApiClient(self._configuration)
        return self._api_client_obj

    @property
    def _dynamic_client(self) -> DynamicClient:
        """The lazily-built dynamic client (does API discovery on first use)."""
        if self._dynamic_client_obj is None:
            self._dynamic_client_obj = DynamicClient(self._api_client)
        return self._dynamic_client_obj

    def _dynamic_api(self, kind: ResourceKind):
        """Resolve the dynamic resource API for a ResourceKind (apiVersion + kind)."""
        return self._dynamic_client.resources.get(api_version=kind.api_version, kind=kind.kind)

    def connect(self) -> None:
        """Eagerly establish the connection (API discovery).

        So the first request doesn't pay for it. Idempotent - a no-op once
        connected. Blocking.
        """
        _ = self._dynamic_client

    def apply(self, manifest: dict) -> list[dict]:
        """Server-side apply a manifest (create-or-update), forcing conflicts.

        Args:
            manifest: The resource manifest dict to apply.

        Returns:
            The applied object(s) as dicts (including server-assigned fields).
        """
        results = utils.create_from_dict(
            self._api_client,
            manifest,
            verbose=False,
            namespace=self._namespace,
            apply=True,
            force_conflicts=True,
            **self._opts,
        )
        return [i.to_dict() for i in results]

    def get(
        self, kind: ResourceKind, name: str | None = None, label_selector: str | None = None
    ) -> dict | list[dict]:
        """Get a resource by name, or list a kind by label selector.

        Args:
            kind: The resource kind to fetch.
            name: The object name for a single get; None to list.
            label_selector: Label selector for the list form.

        Returns:
            The object dict (named get) or a list of object dicts (list form).

        Raises:
            NotFoundError: If a named get returns a 404. Other errors propagate.
        """
        dynamic_api = self._dynamic_api(kind)
        if name is None:
            results = dynamic_api.get(
                namespace=self._namespace, label_selector=label_selector, **self._opts
            )
            return [i.to_dict() for i in results.items]
        try:
            return dynamic_api.get(name=name, namespace=self._namespace, **self._opts).to_dict()
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def list_resources(
        self, kind: ResourceKind, *, label_selector: str | None = None
    ) -> tuple[list[dict], str | None]:
        """List a kind together with the collection's ``resourceVersion``.

        The version is what makes the listing resumable: a watch started from it
        replays everything since, so relist-then-follow has no gap.

        Args:
            kind: The resource kind to list.
            label_selector: Label selector to narrow the listing.

        Returns:
            The objects, and the resourceVersion (None if the server reported
            none, leaving a watch to start from now).
        """
        results = self._dynamic_api(kind).get(
            namespace=self._namespace, label_selector=label_selector, **self._opts
        )
        version = getattr(getattr(results, "metadata", None), "resourceVersion", None)
        return [i.to_dict() for i in results.items], version

    def watch(
        self,
        kind: ResourceKind,
        *,
        resource_version: str | None = None,
        label_selector: str | None = None,
        timeout_seconds: int | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """Follow changes to a kind, yielding ``(event_type, object)`` as they arrive.

        Blocking, and deliberately finite: ``timeout_seconds`` closes the stream
        and ends the iteration, so a caller relists and an expired
        ``resource_version`` heals itself. The per-request read timeout is not
        applied - a watch is idle between events by design.

        Args:
            kind: The resource kind to follow.
            resource_version: Where to resume from, normally the version
                :meth:`list_resources` returned. None starts from now.
            label_selector: Label selector to narrow the stream.
            timeout_seconds: How long the server holds the stream open.

        Yields:
            ``(event_type, object)`` - ADDED/MODIFIED/DELETED and the object.
        """
        stream = self._dynamic_api(kind).watch(
            namespace=self._namespace,
            resource_version=resource_version,
            label_selector=label_selector,
            timeout=timeout_seconds,
        )
        for event in stream:
            obj = event.get("object")
            yield str(event.get("type")), obj.to_dict() if hasattr(obj, "to_dict") else (obj or {})

    def patch(self, kind: ResourceKind, name: str, body: dict) -> dict:
        """Merge-patch one field of an existing resource.

        Deliberately narrow next to :meth:`apply`, which is how everything the
        platform *owns* is written: a full server-side apply is create-or-update
        by construction, where a patch 404s on an absent object. This exists for
        the one thing that is not desired state - annotating a kpack ``Build`` to
        ask for another build - where composing and applying the whole object
        would mean owning a resource kpack creates.

        Merge patch, not strategic: strategic merge is unavailable on custom
        resources.

        Args:
            kind: The resource kind to patch.
            name: The object name.
            body: The merge patch (only the fields being changed).

        Returns:
            The patched object.

        Raises:
            NotFoundError: If the resource does not exist (404). Other errors
                propagate.
        """
        dynamic_api = self._dynamic_api(kind)
        try:
            patched = dynamic_api.patch(
                name=name,
                namespace=self._namespace,
                body=body,
                content_type="application/merge-patch+json",
                **self._opts,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise
        return patched.to_dict()

    def delete(self, kind: ResourceKind, name: str) -> None:
        """Delete a resource by name.

        Args:
            kind: The resource kind to delete.
            name: The object name.

        Raises:
            NotFoundError: If the resource is already absent (404). Other errors
                propagate, so callers can tell "already gone" from a real failure.
        """
        dynamic_api = self._dynamic_api(kind)
        try:
            dynamic_api.delete(name=name, namespace=self._namespace, **self._opts)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def pod_logs(
        self,
        pod: str,
        *,
        container: str,
        since_seconds: int | None = None,
        limit_bytes: int | None = None,
    ) -> str:
        """Read a snapshot of one pod container's current log.

        Uses CoreV1Api directly - the dynamic client can't read the ``log``
        subresource. The returned text is whatever the node currently holds for
        the container (Kubernetes keeps no ring buffer beyond the node's rotated
        log file); it is not a live stream.

        Args:
            pod: The pod name.
            container: The container to read (e.g. the Knative user-container).
            since_seconds: Only return logs newer than this many seconds, if set.
            limit_bytes: Cap the number of bytes returned, if set.

        Returns:
            The log text.

        Raises:
            NotFoundError: If the pod is gone (404). Other errors propagate.
        """
        core = client.CoreV1Api(self._api_client)
        try:
            return core.read_namespaced_pod_log(
                name=pod,
                namespace=self._namespace,
                container=container,
                timestamps=True,
                since_seconds=since_seconds,
                limit_bytes=limit_bytes,
                **self._opts,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"pod '{pod}' not found") from exc
            raise

    def follow_pod_logs(
        self,
        pod: str,
        *,
        container: str,
        since_seconds: int | None = None,
    ) -> "LogFollow":
        """Open a held-open stream of one pod container's log.

        The counterpart to :meth:`pod_logs`, which returns what the node holds
        right now and ends. This one does not end: the API server keeps writing
        as the container writes, so the caller reads until the pod stops, the
        stream is closed, or the connection drops.

        ``_preload_content=False`` is what makes that possible - the generated
        client would otherwise read the whole response into a string before
        returning, which for ``follow=True`` never completes. The raw urllib3
        response comes back instead, and :class:`LogFollow` owns it.

        No read timeout is applied. A follow is idle between lines by design, so
        the per-request timeout that protects an ordinary call would end this one
        on a quiet workload; the caller bounds it instead (its own deadline, and
        :meth:`LogFollow.close`).

        Args:
            pod: The pod name.
            container: The container to read.
            since_seconds: Start this many seconds back, so a client sees recent
                context rather than only what arrives after it connected.

        Returns:
            The open stream.

        Raises:
            NotFoundError: If the pod is gone (404). Other errors propagate.
        """
        core = client.CoreV1Api(self._api_client)
        try:
            response = core.read_namespaced_pod_log(
                name=pod,
                namespace=self._namespace,
                container=container,
                timestamps=True,
                since_seconds=since_seconds,
                follow=True,
                _preload_content=False,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"pod '{pod}' not found") from exc
            raise
        return LogFollow(response)

    def close(self) -> None:
        """Release the underlying HTTP client (connection pool) for this site.

        Idempotent and safe to call at shutdown; the lazy clients are rebuilt on
        next use if the Cluster is reused afterwards.
        """
        if self._api_client_obj is not None:
            self._api_client_obj.close()
            self._api_client_obj = None
        self._dynamic_client_obj = None


class LogFollow:
    """A held-open pod log stream: lines as they arrive, and a way to end it.

    Two threads touch one of these and they do different things. The worker
    thread iterates :meth:`lines`, blocked on the socket in between. The event
    loop calls :meth:`close`, which is the only way to end that block early -
    setting a flag would not, because the flag is only read between lines and a
    quiet workload produces none. Closing the socket makes the pending read fail,
    the iteration stops, and the thread is returned to the pool.
    """

    def __init__(self, response):
        """Wrap the raw streaming response.

        Args:
            response: The urllib3 response from a ``follow=True`` log read.
        """
        self._response = response
        self._closed = False

    def lines(self) -> Iterator[str]:
        """Yield complete log lines as the container writes them (blocking).

        Chunks are reassembled here rather than trusted to arrive line-aligned:
        the transfer is chunked by the API server at whatever boundary it likes,
        so a single write can be split across chunks and several can share one.

        Yields:
            One log line at a time, newline stripped. A trailing partial line is
            emitted when the stream ends, so a final write with no newline is
            not swallowed.
        """
        buffer = ""
        for chunk in self._response.stream(amt=None, decode_content=True):
            if self._closed:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # Not splitlines(): it also breaks on \v, \f and U+2028, any of which
            # a workload may legitimately log inside one line.
            *complete, buffer = buffer.split("\n")
            for line in complete:
                yield line.rstrip("\r")
        if buffer and not self._closed:
            yield buffer.rstrip("\r")

    def close(self) -> None:
        """End the stream, unblocking a thread waiting on it. Idempotent.

        Both calls matter and neither replaces the other: ``close`` drops the
        socket, which is what interrupts the pending read, and ``release_conn``
        is what stops the pool holding a connection that will never be reused.
        Failures are swallowed - this only ever runs while tearing a stream
        down, where there is nothing left to report to.
        """
        self._closed = True
        for end in (self._response.close, self._response.release_conn):
            try:
                end()
            except Exception:  # noqa: BLE001, S110 - teardown; nothing to report to
                pass


def clusters_for(settings: CommonSettings) -> dict[str, Cluster]:
    """One client per configured site, keyed by site name (connections stay lazy).

    Args:
        settings: Shared settings carrying the site list.

    Returns:
        ``{site_name: Cluster}``, empty when no sites are configured.
    """
    return {site.name: Cluster(site, settings) for site in settings.sites}


def select_local(clusters: dict[str, Cluster], local_site: str | None) -> Cluster:
    """The cluster this process sits in, from :func:`clusters_for`'s mapping.

    Matched on the site name first, then the cluster name, so either spelling in
    the chart resolves; falls back to the first site. Shared, because the API
    and the controller mean the same thing by "local".

    Args:
        clusters: The per-site clients.
        local_site: The configured local site (or cluster) name.

    Returns:
        The local cluster.

    Raises:
        ValidationError: If no sites are configured.
    """
    if not clusters:
        raise ValidationError("no sites are configured")
    if local_site:
        by_site = clusters.get(local_site)
        if by_site:
            return by_site
        for cluster in clusters.values():
            if cluster.name == local_site:  # match the cluster name too
                return cluster
    return next(iter(clusters.values()))
