"""Resolve a workload's ``env`` into container env + a backing Secret.

Plain entries stay inline. Entries marked ``secret: true`` have their value moved
into a single per-workload Kubernetes Secret (``{workload}-env``); the container
then reads them via a secretKeyRef so values never appear inline on the KSVC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.common import EnvVar, ValueFrom
from app.services import resources as res


@dataclass
class ResolvedEnv:
    env: list[EnvVar]  # transformed env to put on the container
    backing: list[dict] = field(default_factory=list)  # Secret manifest(s) to create


def env_secret_name(workload: str) -> str:
    return f"{workload}-env"


def resolve_env(
    workload: str, group: str, owner: str, env: list[EnvVar]
) -> ResolvedEnv:
    secret_name = env_secret_name(workload)
    secret_data: dict[str, str] = {}
    transformed: list[EnvVar] = []

    for e in env:
        if e.secret:
            secret_data[e.name] = e.value or ""
            transformed.append(
                EnvVar(
                    name=e.name,
                    valueFrom=ValueFrom(secret=secret_name, key=e.name),
                )
            )
        else:
            transformed.append(e)

    backing: list[dict] = []
    if secret_data:
        backing.append(res.build_secret(secret_name, group, owner, secret_data))
    return ResolvedEnv(env=transformed, backing=backing)
