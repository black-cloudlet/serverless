"""Per-region Kubernetes/OpenShift cluster client (server-side apply, get, watch, delete).

Shared infrastructure: the API and the build controller both reach a cluster the
same way (client-cert mTLS, lazy connect), configured from the shared
:class:`~common.config.CommonSettings`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from kubernetes import client, utils
from kubernetes.dynamic import DynamicClient

from common.cluster.follow import LogFollow
from common.cluster.kinds import ResourceKind
from common.cluster.namespaced_client import NamespacedCluster
from common.cluster.pool import _default_connect_timeout, _keepalive_socket_options
from common.config import CommonSettings, RegionConfig, RegistryConfig
from common.errors import NotFoundError, ValidationError


class Cluster:
    """A single region's cluster connection and resource operations.

    The Kubernetes client is synchronous and the connection is established lazily,
    on first use (docs/ARCHITECTURE.md - Partial-failure semantics).

    A Cluster is the handle callers pass around to mean "this region", and
    :attr:`registry` is that region's registry.

    It is cluster-scoped, not namespace-scoped: every namespaced operation names
    its namespace explicitly. Code working within one namespace binds it once via
    :meth:`in_namespace` and passes the view around.
    """

    def __init__(self, region_config: RegionConfig, settings: CommonSettings):
        """Configure the client for one region (the connection stays lazy).

        Args:
            region_config: The region's name and cluster identifiers.
            settings: Shared connection settings (TLS material, base domain,
                timeouts, registry).
        """
        self.region: str = region_config.name
        self.name: str = region_config.cluster
        # This region's registry, resolved once, not the platform default.
        self.registry: RegistryConfig = settings.registry_for(region_config.name)

        self._configuration = client.Configuration()
        self._configuration.host = f"https://api.{self.name}.{settings.base_domain}:6443"

        self._configuration.ssl_ca_cert = settings.ca_bundle.file
        self._configuration.cert_file = settings.client_cert_file
        self._configuration.key_file = settings.client_key_file

        self._api_client_obj: client.ApiClient | None = None
        self._dynamic_client_obj: DynamicClient | None = None
        # Guards the lazy builds and close(): a Cluster is shared, and its first
        # use routinely happens on several fan-out threads at once.
        self._client_lock = threading.Lock()
        self._connect_timeout: float = settings.cluster_connect_timeout
        self._opts: dict = {
            "_request_timeout": (
                settings.cluster_connect_timeout,
                settings.cluster_read_timeout,
            )
        }

    @property
    def _api_client(self) -> client.ApiClient:
        """The lazily-built Kubernetes API client for this region."""
        built = self._api_client_obj  # fast path: already built, no lock needed
        if built is not None:
            return built
        with self._client_lock:
            if self._api_client_obj is None:
                api_client = client.ApiClient(self._configuration)
                # Gives every request a connect timeout, discovery and the watch
                # included: those pass none of their own. Ordinary calls carry
                # connect and read timeouts through `self._opts`.
                _default_connect_timeout(api_client, self._connect_timeout)
                # TCP keepalive bounds the long-lived streams, which carry no
                # read timeout - see _keepalive_socket_options.
                api_client.rest_client.pool_manager.connection_pool_kw["socket_options"] = (
                    _keepalive_socket_options()
                )
                self._api_client_obj = api_client
            return self._api_client_obj

    @property
    def _dynamic_client(self) -> DynamicClient:
        """The lazily-built dynamic client (does API discovery on first use)."""
        built = self._dynamic_client_obj
        if built is not None:
            return built
        api_client = self._api_client
        with self._client_lock:
            if self._dynamic_client_obj is None:
                self._dynamic_client_obj = DynamicClient(api_client)
            return self._dynamic_client_obj

    def _dynamic_api(self, kind: ResourceKind):
        """Resolve the dynamic resource API for a ResourceKind (apiVersion + kind)."""
        return self._dynamic_client.resources.get(api_version=kind.api_version, kind=kind.kind)

    def connect(self) -> None:
        """Eagerly establish the connection (API discovery).

        Idempotent - a no-op once connected. Blocking.
        """
        _ = self._dynamic_client

    def apply(
        self, manifest: dict, *, namespace: str | None, field_manager: str | None = None
    ) -> list[dict]:
        """Server-side apply a manifest (create-or-update), forcing conflicts.

        Args:
            manifest: The resource manifest dict to apply.
            namespace: The namespace to apply into. None only for a
                cluster-scoped manifest (a Namespace itself), where the value
                is ignored.
            field_manager: The SSA field-manager name to write under. None
                keeps the client library's default; a component whose re-applies
                must remove the fields it no longer declares (the tenant
                controller) passes its own.

        Returns:
            The applied object(s) as dicts (including server-assigned fields).
        """
        if field_manager:
            # utils.create_from_dict hardcodes its own field manager on the
            # SSA call (a second one raises TypeError), so a caller-supplied
            # manager goes through the dynamic client directly.
            api = self._dynamic_client.resources.get(
                api_version=manifest["apiVersion"], kind=manifest["kind"]
            )
            resp = api.server_side_apply(
                body=manifest,
                namespace=namespace,
                field_manager=field_manager,
                force_conflicts=True,
                **self._opts,
            )
            return [resp.to_dict()]
        results = utils.create_from_dict(
            self._api_client,
            manifest,
            verbose=False,
            namespace=namespace,
            apply=True,
            force_conflicts=True,
            **self._opts,
        )
        return [i.to_dict() for i in results]

    def get(
        self,
        kind: ResourceKind,
        name: str | None = None,
        label_selector: str | None = None,
        *,
        namespace: str | None,
        field_selector: str | None = None,
    ) -> dict | list[dict]:
        """Get a resource by name, or list a kind by label and field selector.

        Args:
            kind: The resource kind to fetch.
            name: The object name for a single get; None to list.
            label_selector: Label selector for the list form.
            namespace: The namespace to read. None lists across *all*
                namespaces (or addresses a cluster-scoped kind); a named get
                of a namespaced kind needs a real namespace.
            field_selector: Field selector for the list form, e.g.
                ``metadata.name=x``. The apiserver applies it, so the listing
                comes back already narrowed.

        Returns:
            The object dict (named get) or a list of object dicts (list form).

        Raises:
            NotFoundError: If a named get returns a 404. Other errors propagate.
        """
        dynamic_api = self._dynamic_api(kind)
        if name is None:
            results = dynamic_api.get(
                namespace=namespace,
                label_selector=label_selector,
                field_selector=field_selector,
                **self._opts,
            )
            return [i.to_dict() for i in results.items]
        try:
            return dynamic_api.get(name=name, namespace=namespace, **self._opts).to_dict()
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def list_resources(
        self,
        kind: ResourceKind,
        *,
        namespace: str | None,
        label_selector: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """List a kind together with the collection's ``resourceVersion``.

        The version is what makes the listing resumable: a watch started from it
        replays everything since, so relist-then-follow has no gap.

        Args:
            kind: The resource kind to list.
            namespace: The namespace to list. None lists across all namespaces.
            label_selector: Label selector to narrow the listing.

        Returns:
            The objects, and the resourceVersion (None if the server reported
            none, leaving a watch to start from now).
        """
        results = self._dynamic_api(kind).get(
            namespace=namespace, label_selector=label_selector, **self._opts
        )
        version = getattr(getattr(results, "metadata", None), "resourceVersion", None)
        return [i.to_dict() for i in results.items], version

    def watch(
        self,
        kind: ResourceKind,
        *,
        namespace: str | None,
        resource_version: str | None = None,
        label_selector: str | None = None,
        timeout_seconds: int | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """Follow changes to a kind, yielding ``(event_type, object)`` as they arrive.

        Blocking and finite: ``timeout_seconds`` closes the stream and ends the
        iteration, after which a caller relists and watches again
        (docs/BUILDING.md - One pass). No read timeout applies to a watch: the
        per-request timeouts in ``self._opts`` are not passed, the default
        installed in ``_api_client`` bounds only the connect, and the dynamic
        client's ``watch`` accepts no per-request override.

        Args:
            kind: The resource kind to follow.
            namespace: The namespace to watch. None follows all namespaces -
                one stream, however many namespaces the objects live in.
            resource_version: Where to resume from, normally the version
                :meth:`list_resources` returned. None starts from now.
            label_selector: Label selector to narrow the stream.
            timeout_seconds: How long the server holds the stream open.

        Yields:
            ``(event_type, object)`` - ADDED/MODIFIED/DELETED and the object.
        """
        stream = self._dynamic_api(kind).watch(
            namespace=namespace,
            resource_version=resource_version,
            label_selector=label_selector,
            timeout=timeout_seconds,
        )
        for event in stream:
            obj = event.get("object")
            yield str(event.get("type")), obj.to_dict() if hasattr(obj, "to_dict") else (obj or {})

    def patch(self, kind: ResourceKind, name: str, body: dict, *, namespace: str | None) -> dict:
        """Merge-patch one field of an existing resource.

        Everything the platform owns is written with :meth:`apply` instead
        (docs/BUILDING.md - Ownership: API vs Build Service); this carries the
        one write that is not desired state - annotating a kpack ``Build`` to
        ask for another build. A patch 404s on an absent object where an apply
        would create it.

        Merge patch, not strategic: strategic merge is unavailable on custom
        resources.

        Args:
            kind: The resource kind to patch.
            name: The object name.
            body: The merge patch (only the fields being changed).
            namespace: The object's namespace (None for a cluster-scoped kind).

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
                namespace=namespace,
                body=body,
                content_type="application/merge-patch+json",
                **self._opts,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise
        return patched.to_dict()

    def delete(self, kind: ResourceKind, name: str, *, namespace: str | None) -> None:
        """Delete a resource by name.

        Args:
            kind: The resource kind to delete.
            name: The object name.
            namespace: The object's namespace (None for a cluster-scoped kind).

        Raises:
            NotFoundError: If the resource is already absent (404). Other errors
                propagate, so callers can tell "already gone" from a real failure.
        """
        dynamic_api = self._dynamic_api(kind)
        try:
            dynamic_api.delete(name=name, namespace=namespace, **self._opts)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def pod_logs(
        self,
        pod: str,
        *,
        namespace: str,
        container: str,
        since_seconds: int | None = None,
        limit_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> str:
        """Read a snapshot of one pod container's current log.

        Uses CoreV1Api directly - the dynamic client can't read the ``log``
        subresource. The returned text is whatever the node currently holds for
        the container (Kubernetes keeps no ring buffer beyond the node's rotated
        log file); it is not a live stream.

        Args:
            pod: The pod name.
            namespace: The pod's namespace (a pod read is never cluster-wide).
            container: The container to read (e.g. the Knative user-container).
            since_seconds: Only return logs newer than this many seconds, if set.
            limit_bytes: Cap the number of bytes returned, if set. Kubernetes
                truncates from the *start* of the selected window, so combine
                with ``tail_lines`` when the newest lines are the ones wanted.
            tail_lines: Only return the newest this-many lines, if set.

        Returns:
            The log text.

        Raises:
            NotFoundError: If the pod is gone (404). Other errors propagate.
        """
        core = client.CoreV1Api(self._api_client)
        try:
            return core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                timestamps=True,
                since_seconds=since_seconds,
                limit_bytes=limit_bytes,
                tail_lines=tail_lines,
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
        namespace: str,
        container: str,
        since_seconds: int | None = None,
        tail_lines: int | None = None,
    ) -> "LogFollow":
        """Open a held-open stream of one pod container's log.

        The counterpart to :meth:`pod_logs`, which returns what the node holds
        right now and ends. This one does not end: the API server keeps writing
        as the container writes, so the caller reads until the pod stops, the
        stream is closed, or the connection drops.

        ``_preload_content=False`` is what makes that possible: the generated
        client otherwise reads the whole response into a string before
        returning, which for ``follow=True`` never completes. The raw urllib3
        response comes back instead, and :class:`LogFollow` owns it.

        No read timeout is applied. The caller bounds the stream instead - its
        own deadline, and :meth:`LogFollow.close`
        (docs/ARCHITECTURE.md - A held-open stream holds a thread).

        Args:
            pod: The pod name.
            namespace: The pod's namespace.
            container: The container to read.
            since_seconds: Start this many seconds back, if set.
            tail_lines: Start at the newest this-many lines instead, however old
                they are, if set.

        Returns:
            The open stream.

        Raises:
            NotFoundError: If the pod is gone (404). Other errors propagate.
        """
        core = client.CoreV1Api(self._api_client)
        try:
            response = core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                timestamps=True,
                since_seconds=since_seconds,
                tail_lines=tail_lines,
                follow=True,
                _preload_content=False,
                # Connect timeout only; the read side stays unbounded, as
                # documented above.
                _request_timeout=(self._connect_timeout, None),
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"pod '{pod}' not found") from exc
            raise
        return LogFollow(response)

    def in_namespace(self, namespace: str) -> NamespacedCluster:
        """A view of this cluster with ``namespace`` bound into every operation.

        Args:
            namespace: The namespace every operation on the view targets.

        Returns:
            The bound view. The connection stays this Cluster's; the view
            holds none of its own.
        """
        return NamespacedCluster(self, namespace)

    def close(self) -> None:
        """Release the underlying HTTP client (connection pool) for this region.

        Idempotent and safe to call at shutdown; the lazy clients are rebuilt on
        next use if the Cluster is reused afterwards.
        """
        with self._client_lock:
            api_client, self._api_client_obj = self._api_client_obj, None
            self._dynamic_client_obj = None
        if api_client is not None:
            api_client.close()


def clusters_for(settings: CommonSettings) -> dict[str, Cluster]:
    """One client per configured region, keyed by region name (connections stay lazy).

    Args:
        settings: Shared settings carrying the region list.

    Returns:
        ``{region_name: Cluster}``, empty when no regions are configured.
    """
    return {region.name: Cluster(region, settings) for region in settings.regions}


def select_local(clusters: dict[str, Cluster], local_region: str | None) -> Cluster:
    """The cluster this process sits in, from :func:`clusters_for`'s mapping.

    Matched on the region name first, then the cluster name, so either spelling in
    the chart resolves (docs/ARCHITECTURE.md - Multi-Region). A configured name
    that matches nothing raises rather than falling back; only an *unset* name
    falls back, to the first configured region.

    Args:
        clusters: The per-region clients.
        local_region: The configured local region (or cluster) name.

    Returns:
        The local cluster.

    Raises:
        ValidationError: If no regions are configured, or ``local_region`` names one
            that is not.
    """
    if not clusters:
        raise ValidationError("no regions are configured")
    if local_region:
        by_region = clusters.get(local_region)
        if by_region:
            return by_region
        for cluster in clusters.values():
            if cluster.name == local_region:  # match the cluster name too
                return cluster
        raise ValidationError(
            f"local region '{local_region}' matches none of the configured regions: "
            f"{sorted(clusters)}"
        )
    return next(iter(clusters.values()))
