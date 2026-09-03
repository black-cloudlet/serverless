"""The checks that run before a workload is written, and what they refuse.

Everything here answers "may this deploy proceed?" and nothing here mutates.
One rule holds across all of them: **a check that could not be run has not
passed**. An unreachable region cannot prove a host is free, so these fail closed
with a 503 - see :func:`assert_all_regions_checked`
(docs/API.md - Partial-failure semantics).

:class:`~api.services.workloads.WorkloadService` exposes these as methods.
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
        # The name/group pair rule, checked where the default host label is built.
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

    Runs the in-memory resolution the apply will later perform, so bad input fails
    as a 400 at accept time rather than in the background deploy that follows the
    202.

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
    # The name/group pair is not checked here: it binds the default host, not
    # the object name, so `resolve_host` checks it.
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

    Both questions are answered in ONE visit per region, so a region's two
    answers describe the same instant.

    Only a real 404 means free/absent. An unreachable region cannot prove either,
    so this fails closed with a 503 (docs/API.md - Partial-failure
    semantics).

    The check is not atomic with the write: the apply that follows is a separate
    operation, so a peer can claim the host in between. The apply path runs this
    again immediately before mutating instead of trusting the accept-time result.

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
        if host is not None:
            owner = _host_owner(cluster, host, name, group)
            if owner is not None:
                return RegionStatus(region=cluster.region, status="Taken", message=owner)
        if require_absent:
            # Namespace-scoped: the same name in another group is a different
            # workload, living in another namespace.
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, name)
                return RegionStatus(region=cluster.region, status="Exists")
            except NotFoundError:
                pass
        return RegionStatus(region=cluster.region, status="Available")

    statuses = await deployer.fanout(targets, probe)
    # A host conflict is reported ahead of a name conflict.
    taken = next((s for s in statuses if s.status == "Taken"), None)
    if taken is not None:
        raise ConflictError(f"hostname '{host}' is already assigned to {taken.message}")
    if any(s.status == "Exists" for s in statuses):
        raise ConflictError(f"workload '{name}' already exists")
    assert_all_regions_checked(statuses, f"verify workload '{name}' can be deployed")


def _host_owner(cluster: NamespacedCluster, host: str, name: str, group: str) -> str | None:
    """Whether ``host`` already belongs to a workload other than this one.

    Cluster-scoped, not namespace-scoped: a host is a DNS name, so it is unique
    across the whole platform, and with a namespace per group the workload
    holding it may live in someone else's. A get by name cannot span namespaces,
    so this lists the platform's own DomainMappings and matches on the name.

    The owner is identified by workload AND group: the workload label alone is
    not unique across groups, so a mapping counts as this workload's own only
    when both labels match.

    Args:
        cluster: The region to ask, as a namespace-bound view - the underlying
            cluster is used directly, because this question is not namespaced.
        host: The host being claimed (and the DomainMapping's name).
        name: The workload claiming it.
        group: That workload's group.

    Returns:
        A description of the workload holding the host, or None when it is
        free or already this workload's own.
    """
    # Both selectors are applied by the apiserver; the field selector narrows the
    # cluster-wide list to the single object that could answer.
    mappings = cluster.cluster.get(
        ResourceKind.DOMAIN_MAPPING,
        label_selector=f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE}",
        field_selector=f"metadata.name={host}",
        namespace=None,
    )
    # Same-named mappings can coexist across namespaces (Knative marks the
    # loser DomainAlreadyClaimed, but the object exists), so the verdict must
    # come from the whole listing, in whatever order the apiserver returns it:
    # the caller's OWN mapping anywhere means available - a leftover loser
    # object must not lock the winner out of its own updates - and otherwise
    # any foreign mapping means taken.
    foreign = None
    for mapping in mappings:
        meta = mapping.get("metadata") or {}
        labels = meta.get("labels") or {}
        if labels.get(LABEL_WORKLOAD) == name and labels.get(LABEL_GROUP) == group:
            return None
        # Report which workload holds it and where: the owner can live in a
        # namespace the caller cannot see.
        if foreign is None:
            foreign = (
                f"{labels.get(LABEL_WORKLOAD) or '?'} in namespace {meta.get('namespace') or '?'}"
            )
    return foreign


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
