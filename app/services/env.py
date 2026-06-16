"""Resolve a workload's ``env`` into container env + a backing Secret.

Plain entries stay inline. Entries marked ``secret: true`` have their value moved
into a single per-workload Kubernetes Secret (``{workload}-env``); the container
then reads them via a secretKeyRef so values never appear inline on the KSVC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.common import EnvVar
from app.services import resources as res
from app.services.ksvc import ContainerEnv
from app.services.labels import workload_labels


@dataclass
class ResolvedEnv:
    env: list[ContainerEnv]  # resolved container env entries
    backing: list[dict] = field(default_factory=list)  # Secret manifest(s) to create


def env_secret_name(workload: str) -> str:
    return f"{workload}-env"


def resolve_env(
    workload: str, group: str, owner: str, env: list[EnvVar]
) -> ResolvedEnv:
    secret_name = env_secret_name(workload)
    secret_data: dict[str, str] = {}
    resolved: list[ContainerEnv] = []

    for e in env:
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
