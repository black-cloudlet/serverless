"""Resolve a workload's ``env`` into container env + a backing Secret."""

from __future__ import annotations

from dataclasses import dataclass, field

from api.models.common import EnvVar
from api.services.manifests import resources as res
from api.services.manifests.ksvc import ContainerEnv
from common.errors import ValidationError
from common.labels import workload_labels


@dataclass
class ResolvedEnv:
    """Resolved env: container entries plus the backing Secret, if any."""

    env: list[ContainerEnv]  # resolved container env entries
    backing: list[dict] = field(default_factory=list)  # Secret manifest(s) to create


def env_secret_name(workload: str) -> str:
    """The name of a workload's env Secret: ``{workload}-env``."""
    return f"{workload}-env"


def resolve_env(
    workload: str,
    group: str,
    owner: str,
    env: list[EnvVar],
    kept: dict[str, str] | None = None,
) -> ResolvedEnv:
    """Resolve env vars into container entries and a backing Secret.

    Raises:
        ValidationError: If two env vars share a name, or a secret var is sent with
            no value and none is stored to keep.
    """
    kept = kept or {}
    secret_name = env_secret_name(workload)
    secret_data: dict[str, str] = {}
    resolved: list[ContainerEnv] = []
    seen: set[str] = set()

    for e in env:
        if e.name in seen:
            raise ValidationError(f"duplicate env variable name '{e.name}'")
        seen.add(e.name)
        if e.secret:
            value = e.value if e.value is not None else kept.get(e.name)
            if value is None:
                raise ValidationError(
                    f"secret env var '{e.name}' has no value and none is stored to keep"
                )
            secret_data[e.name] = value
            resolved.append(ContainerEnv(name=e.name, secret_ref=(secret_name, e.name)))
        else:
            resolved.append(ContainerEnv(name=e.name, value=e.value))

    backing: list[dict] = []
    if secret_data:
        labels = workload_labels(group, owner, workload)
        backing.append(res.build_secret(secret_name, labels, secret_data))
    return ResolvedEnv(env=resolved, backing=backing)
