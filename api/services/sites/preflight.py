"""The checks that run before a workload is written, and what they refuse.

An unreachable site cannot prove a host is free, so these fail closed with a 503
rather than treating silence as consent.
"""

from __future__ import annotations

from api.models.common import LABEL_WORKLOAD, SiteStatus
from api.services.manifests import route as route_svc
from api.services.manifests.env import resolve_env
from api.services.manifests.files import resolve_files
from api.services.sites.deployer import Deployer
from common.cluster import Cluster, ResourceKind
from common.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from common.names import object_name, validate_object_name


def resolve_host(name: str, hostname: str | None, group: str, domain: str) -> str:
    """Resolve the external host for a workload, validating any custom one.

    Raises:
        ValidationError: If a custom host isn't exactly one label under the
            platform base domain.
    """
    if not hostname:
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

    Raises:
        ValidationError: If the env or files cannot be resolved, or if the
            name and group are too long together to be a DNS label.
    """
    try:
        oname = validate_object_name(name, group)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    resolve_files(oname, group, owner, files, kept_files)
    resolve_env(oname, group, owner, env, kept_env)


async def assert_deployable(
    deployer: Deployer,
    name: str,
    group: str,
    targets: list[Cluster],
    *,
    host: str | None = None,
    require_absent: bool = False,
) -> None:
    """Assert a workload can be deployed: host free, and optionally name unused.

    Raises:
        ConflictError: If the host belongs to another workload, or the name is
            already taken.
        ServiceUnavailableError: If any site was unreachable.
    """
    oname = object_name(name, group)

    def probe(cluster: Cluster) -> SiteStatus:
        if host is not None:
            try:
                existing = cluster.get(ResourceKind.DOMAIN_MAPPING, host)
            except NotFoundError:
                existing = None
            if existing is not None:
                labels = (existing.get("metadata", {}) or {}).get("labels", {}) or {}
                # The workload's own mapping counts as available (update path).
                if labels.get(LABEL_WORKLOAD) != oname:
                    return SiteStatus(site=cluster.site, status="Taken")
        if require_absent:
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
                return SiteStatus(site=cluster.site, status="Exists")
            except NotFoundError:
                pass
        return SiteStatus(site=cluster.site, status="Available")

    statuses = await deployer.fanout(targets, probe)
    # The host conflict is reported first: it is the one an idempotent apply
    # would silently resolve by hijacking another workload's mapping.
    if any(s.status == "Taken" for s in statuses):
        raise ConflictError(f"hostname '{host}' is already assigned")
    if any(s.status == "Exists" for s in statuses):
        raise ConflictError(f"workload '{name}' already exists")
    assert_all_sites_checked(statuses, f"verify workload '{name}' can be deployed")


def assert_all_sites_checked(statuses: list[SiteStatus], action: str) -> None:
    """Fail closed if any site could not be reached during a conflict check.

    A missing answer is not evidence of "no conflict".

    Raises:
        ServiceUnavailableError: If any site reported an error.
    """
    unreachable = [s.site for s in statuses if s.error is not None]
    if unreachable:
        raise ServiceUnavailableError(
            f"cannot {action}: site(s) unreachable: {', '.join(sorted(unreachable))}"
        )
