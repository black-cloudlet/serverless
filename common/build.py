"""The build domain shared by the API (client) and the build service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic.dataclasses import dataclass as validated_dataclass

from common.names import (
    Branch,
    GitUrl,
    Group,
    Name,
    SourcePath,
    cache_repository,
    image_repository,
    image_tag,
)

if TYPE_CHECKING:  # a type hint only - importing it would pull the k8s client
    from common.cluster import Cluster


@validated_dataclass
class BuildRequest:
    """Inputs for building a function image from source."""

    name: Name
    group: Group
    git_url: GitUrl
    branch: Branch
    git_token: str
    runtime: str
    version: str | None = None
    path: SourcePath = ""
    owner: str = ""
    revision: str | None = None

    @property
    def build_revision(self) -> str:
        """The git revision to build: the pinned commit, else the branch."""
        return self.revision or self.branch


@dataclass
class BuildPlan:
    """What declaring a build produces, split by how far each piece travels."""

    tag: str
    replicated: list[dict]
    local: list[dict]


@dataclass
class BuildStatus:
    """A function's current build state, read back from the build backend."""

    state: str
    image: str | None = None
    message: str | None = None


class BuildBackend(Protocol):
    """Declares function builds and reports their state."""

    @property
    def pull_secret(self) -> str | None:
        """The registry Secret a built function's KSVC pulls its image with.

        None when the platform configures no registry credential, so the KSVC
        references no Secret the chart never created.
        """
        ...

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference the build will push to (deterministic, no I/O)."""
        ...

    def plan(self, req: BuildRequest, labels: dict[str, str]) -> BuildPlan:
        """The manifests declaring the build, split by replication scope."""
        ...

    def trigger(self, cluster: Cluster, name: str, group: str) -> bool:
        """Ask for one more build of inputs that have not changed.

        The only imperative call here - re-applying matching desired state is a
        no-op no backend rebuilds from. Call it after applying the plan.
        """
        ...

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...

    def statuses(self, cluster: Cluster, group: str) -> dict[str, BuildStatus]:
        """Every build state a group has on one cluster, keyed by workload object name."""
        ...


def image_reference(registry_base: str, req: BuildRequest) -> str:
    """The image reference convention for a build: ``{base}/{group}/{name}:{tag}``.

    Shared so the API and the build backend agree on where a build's image lands.
    """
    base = registry_base.rstrip("/")
    return f"{base}/{image_repository(req.group, req.name)}:{image_tag(req.branch)}"


def cache_reference(registry_base: str, req: BuildRequest) -> str:
    """Where a build's layer cache lives: ``{base}/{group}/{name}_cache:latest``.

    The registry form of kpack's cache, preferred over the volume form because
    that one is a PVC per function (docs/BUILDING.md - Build cache).
    """
    base = registry_base.rstrip("/")
    return f"{base}/{cache_repository(req.group, req.name)}:latest"
