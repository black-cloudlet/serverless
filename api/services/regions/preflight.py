"""The checks that run before a workload is written, and what they refuse.

Everything here answers "may this deploy proceed?" and nothing here mutates.
They are grouped because they share one rule that is easy to lose when it is
spread across an orchestrator: **a check that could not be run has not passed**.
An unreachable region cannot prove a host is free, so these fail closed with a 503
rather than reading silence as consent - see :func:`assert_all_regions_checked`.

:class:`~api.services.workloads.WorkloadService` exposes these as methods; the
logic lives here so it can be read (and tested) without the deploy path around it.
"""

from __future__ import annotations

from api.models.common import (
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
    RegionStatus,
)
from api.services.manifests import route as route_svc
from api.services.manifests.env import resolve_env
from api.services.manifests.files import resolve_files
from api.services.regions.deployer import Deployer
from common.cluster import NamespacedCluster, ResourceKind
from common.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from common.names import validate_default_host_label


def resolve_host(name: str, hostname: str | None, group: str, domain: str) -> str:
    """Resolve the external host for a workload, validating any custom one.

    - no hostname -> the default ``{name}-{group}.{route_domain}``
    - a single label -> the base domain is appended (``{label}.{route_domain}``)
    - an FQDN -> accepted only if it is exactly one label under the base
      domain (``{label}.{route_domain}``); deeper names are rejected

    Args:
        name: The workload name.
        hostname: The caller-supplied custom host, or None for the default.
        group: The owning group.
        domain: The platform's base route domain.

    Returns:
        The resolved external host.

    Raises:
        ValidationError: If a custom host isn't exactly one label under the
            platform base domain, or if the default host's ``{name}-{group}``
            label would be too long (pass a hostname instead).
    """
    if not hostname:
        # The pair rule, at the one place the pair is still written. A name and
        # group that no longer fit together are now only a reason to supply a
        # hostname, not a reason the workload cannot exist.
        try:
            validate_default_host_label(name, group)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return route_svc.host_for(name, group, domain)
    if "." not in hostname:
        label = hostname
    elif hostname.endswith(f".{domain}"):
        label = hostname[: -len(domain) - 1]  # strip ".{domain}"
    else:
        raise ValidationError(f"hostname must be a single label under '{domain}'")
    if not label or "." in label:
        raise ValidationError(f"hostname must be exactly one label under '{domain}'")
    return f"{label}.{domain}"


def validate_spec(
    name: str,
    group: str,
    owner: str,
    env,
    files,
    kept_env: dict[str, str] | None = None,
    kept_files: dict[str, bytes] | None = None,
) -> None:
    """Validate a spec synchronously, before the request is accepted.

    Runs the in-memory resolution the apply will later perform, so bad input fails as
    a 400 at accept time instead of being accepted (202) and dying silently in the
    background deploy.

    Args:
        name: Workload name.
        group: Owning group.
        owner: Username stamped on derived resources.
        env: The submitted env vars.
        files: The submitted file mounts.
        kept_env: Existing env-Secret values a "keep" secret falls back on
            (update only; empty/None on create).
        kept_files: Existing files-Secret values a "keep" secret file falls back
            on (update only; empty/None on create).

    Raises:
        ValidationError: If the env or files cannot be resolved.
    """
    # The name/group pair is no longer checked here: it binds the default host,
    # not the object name, so `resolve_host` is where it belongs now.
    resolve_files(name, group, owner, files, kept_files)
    resolve_env(name, group, owner, env, kept_env)


async def assert_deployable(
    deployer: Deployer,
    name: str,
    group: str,
    targets: list[NamespacedCluster],
    *,
    host: str | None = None,
    require_absent: bool = False,
) -> None:
    """Assert a workload can be deployed: host free, and optionally name unused.

    Both questions are answered in ONE visit per region. They used to be two
    separate fan-outs, which cost two cross-region round trips per deploy and -
    worse - described two different instants; asking together means a region's
    two answers cannot disagree about the moment they were taken.

    Only a real 404 means free/absent. An unreachable region can't prove either,
    so this fails closed (503) rather than treating silence as consent -
    otherwise a create against a down peer could hijack its DomainMapping or
    overwrite a workload it is still serving.

    This does not make the deploy atomic, and is not meant to: the apply that
    follows is a separate operation, so a peer can still claim the host in
    between. It is the guard that makes that window small and the failure
    loud, which is why the apply path runs it again immediately before
    mutating rather than trusting the accept-time result.

    Args:
        deployer: The multi-region fan-out helper.
        name: The workload name claiming the host.
        group: The workload's owning group.
        targets: The clusters to check.
        host: The external host (== the DomainMapping name); None skips the
            host check (nothing is claiming a host).
        require_absent: Also require that no workload of this name exists
            (create only - an update is expected to find its own).

    Raises:
        ConflictError: If the host belongs to another workload, or the name is
            already taken.
        ServiceUnavailableError: If any region was unreachable.
    """

    def probe(cluster: NamespacedCluster) -> RegionStatus:
        if host is not None and _host_taken(cluster, host, name, group):
            return RegionStatus(region=cluster.region, status="Taken")
        if require_absent:
            # Namespace-scoped, and that is the point: the same name in another
            # group is a different workload now, living in another namespace.
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, name)
                return RegionStatus(region=cluster.region, status="Exists")
            except NotFoundError:
                pass
        return RegionStatus(region=cluster.region, status="Available")

    statuses = await deployer.fanout(targets, probe)
    # The host conflict is reported first: it is the one an idempotent apply
    # would silently resolve by hijacking another workload's mapping.
    if any(s.status == "Taken" for s in statuses):
        raise ConflictError(f"hostname '{host}' is already assigned")
    if any(s.status == "Exists" for s in statuses):
        raise ConflictError(f"workload '{name}' already exists")
    assert_all_regions_checked(statuses, f"verify workload '{name}' can be deployed")


def _host_taken(cluster: NamespacedCluster, host: str, name: str, group: str) -> bool:
    """Whether ``host`` already belongs to a workload other than this one.

    Cluster-scoped, not namespace-scoped: a host is a DNS name, so it is unique
    across the whole platform, and with a namespace per group the workload
    holding it may live in someone else's. A get by name cannot span namespaces,
    so this lists the platform's own DomainMappings and matches on the name.

    The owner is identified by workload AND group. The workload label alone
    stopped being unique the moment two groups could each have an ``app``, and
    "is this my own mapping?" answered on the workload label alone would let one
    group's update quietly adopt another's host.

    Args:
        cluster: The region to ask, as a namespace-bound view - the underlying
            cluster is used directly, because this question is not namespaced.
        host: The host being claimed (and the DomainMapping's name).
        name: The workload claiming it.
        group: That workload's group.

    Returns:
        True if another workload holds the host.
    """
    selector = f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE}"
    mappings = cluster.cluster.get(
        ResourceKind.DOMAIN_MAPPING, label_selector=selector, namespace=None
    )
    for mapping in mappings:
        meta = mapping.get("metadata") or {}
        if meta.get("name") != host:
            continue
        labels = meta.get("labels") or {}
        # The workload's own mapping counts as available (the update path).
        return labels.get(LABEL_WORKLOAD) != name or labels.get(LABEL_GROUP) != group
    return False


def assert_all_regions_checked(statuses: list[RegionStatus], action: str) -> None:
    """Fail closed if any region could not be reached during a conflict check.

    A missing answer is not evidence of "no conflict".

    Args:
        statuses: The per-region results of the conflict check.
        action: Human phrase describing the check, for the error message.

    Raises:
        ServiceUnavailableError: If any region reported an error.
    """
    unreachable = [s.region for s in statuses if s.message is not None]
    if unreachable:
        raise ServiceUnavailableError(
            f"cannot {action}: region(s) unreachable: {', '.join(sorted(unreachable))}"
        )
