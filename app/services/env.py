"""Resolve a workload's ``env`` into container env + a backing Secret.

Plain entries stay inline. Entries marked ``secret: true`` have their value moved
into a single per-workload Kubernetes Secret (``{workload}-env``); the container
then reads them via a secretKeyRef so values never appear inline on the KSVC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import ValidationError
from app.models.common import EnvVar
from app.services import resources as res
from app.services.ksvc import ContainerEnv
from app.services.labels import workload_labels


@dataclass
class ResolvedEnv:
    """Resolved env: container entries plus the backing Secret, if any.

    Attributes:
        env: The resolved container env entries (inline or secretKeyRef).
        backing: The ``{workload}-env`` Secret manifest(s) to create, if any.
    """

    env: list[ContainerEnv]  # resolved container env entries
    backing: list[dict] = field(default_factory=list)  # Secret manifest(s) to create


def env_secret_name(workload: str) -> str:
    """The name of a workload's env Secret: ``{workload}-env``."""
    return f"{workload}-env"


def resolve_env(
    workload: str, group: str, owner: str, env: list[EnvVar]
) -> ResolvedEnv:
    """Resolve env vars into container entries and a backing Secret.

    Plain entries stay inline; ``secret: true`` values move into the
    ``{workload}-env`` Secret and are read via a secretKeyRef.

    Args:
        workload: The object name (``{name}-{group}``).
        group: Owning group (for labels).
        owner: Username (for labels).
        env: The submitted env vars.

    Returns:
        The resolved container env and the backing Secret (if any).

    Raises:
        ValidationError: If two env vars share a name.
    """
    secret_name = env_secret_name(workload)
    secret_data: dict[str, str] = {}
    resolved: list[ContainerEnv] = []
    seen: set[str] = set()

    for e in env:
        if e.name in seen:
            raise ValidationError(f"duplicate env variable name '{e.name}'")
        seen.add(e.name)
        if e.secret:
            secret_data[e.name] = e.value
            resolved.append(
                ContainerEnv(name=e.name, secret_ref=(secret_name, e.name))
            )
        else:
            resolved.append(ContainerEnv(name=e.name, value=e.value))

    backing: list[dict] = []
    if secret_data:
        labels = workload_labels(group, owner, workload)
        backing.append(res.build_secret(secret_name, labels, secret_data))
    return ResolvedEnv(env=resolved, backing=backing)
