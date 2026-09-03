"""The build domain shared by the API (client) and the build service.

Both sides import these types, so the request/response shape cannot drift between
the caller and the backend. :class:`BuildBackend` is the protocol they meet on,
implemented by the in-process ``KpackBackend``.

``BuildBackend`` is not kpack's ``Builder`` CR, which is the buildpack
composition an ``Image`` references by name (docs/BUILDING.md - Buildpack
Topology).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic.dataclasses import dataclass as validated_dataclass

from common.config import RegistryConfig
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
    from common.cluster import NamespacedCluster


@validated_dataclass
class BuildRequest:
    """Inputs for building a function image from source.

    Validated on construction: these fields become Kubernetes object names and
    an image reference, and the build path is reachable off the HTTP edge, where
    request-model validation has not run.

    Attributes:
        name: Workload name (DNS-1123 label).
        group: Owning group (DNS-1123 label, normalised).
        git_url: Source repository URL.
        branch: Branch to build.
        path: Directory inside the repository holding the application; ""
            builds from the repository root.
        git_token: The caller's git token, stored for kpack to clone with.
        runtime: Function runtime (python/go/node).
        version: Language version to build with, from the runtime's advertised
            list. None means the platform default for that runtime - which is
            still written explicitly into the build env, never left to the
            buildpack.
        owner: Creating username, stamped on the build objects' labels.
        revision: Exact commit to build. None builds the branch head, which is
            what create and update do; a pinned SHA builds exactly that commit,
            whatever the branch has moved to.
    """

    name: Name
    group: Group
    git_url: GitUrl
    branch: Branch
    # repr=False: a credential must not ride along into log lines, tracebacks
    # or validation errors that print the request.
    git_token: str = field(repr=False)
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
class RegionBuild:
    """One region's half of a build plan.

    Attributes:
        tag: The image reference this region's build pushes to, in its own
            registry.
        manifests: Its ``Image`` and build ServiceAccount, in dependency order.
    """

    tag: str
    manifests: list[dict]


@dataclass
class BuildPlan:
    """What declaring a build produces, split by how far each piece travels.

    Attributes:
        replicated: Manifests every region needs. The git credential lives here,
            so a region rebuilds from a token it already holds
            (docs/BUILDING.md - Active/Active).
        per_region: The build objects each region applies, keyed by region name.
            Each region pushes to its own registry, so the tag differs per
            region (docs/BUILDING.md - Registry layout).
    """

    replicated: list[dict]
    per_region: dict[str, RegionBuild]

    def tag_for(self, region: str) -> str | None:
        """The image reference ``region`` builds to, or None if it does not build.

        Args:
            region: The region name.

        Returns:
            The tag, or None when the plan does not cover that region.
        """
        build = self.per_region.get(region)
        return build.tag if build else None

    def manifests_for(self, region: str) -> list[dict]:
        """The build manifests ``region`` applies, empty if it does not build.

        Args:
            region: The region name.

        Returns:
            The manifests, in dependency order.
        """
        build = self.per_region.get(region)
        return build.manifests if build else []

    @property
    def tags(self) -> dict[str, str]:
        """The image reference each region builds to, keyed by region name."""
        return {region: build.tag for region, build in self.per_region.items()}

    @property
    def manifests_by_region(self) -> dict[str, list[dict]]:
        """Each region's build manifests, keyed by region name."""
        return {region: build.manifests for region, build in self.per_region.items()}


@dataclass
class BuildStatus:
    """A function's current build state, read back from the build backend.

    Attributes:
        state: ``Building`` / ``Ready`` / ``Failed`` / ``Unknown``.
        image: The last successfully built image, when known. May lag ``state``:
            a failed rebuild still reports the previous good image.
        message: Why the build failed, when it did.
    """

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

    def image_ref(self, req: BuildRequest, registry: RegistryConfig) -> str:
        """The image reference a build pushes to (deterministic, no I/O)."""
        ...

    def plan(
        self, req: BuildRequest, labels: dict[str, str], registries: Mapping[str, RegistryConfig]
    ) -> BuildPlan:
        """The manifests declaring the build, split by replication scope.

        Pure: returning does not mean an image exists, or even that anything has
        been applied. A backend that builds asynchronously (kpack) describes the
        desired state and lets the caller apply it as owned resources of the
        workload.

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.
            registries: The registry each building region pushes to, keyed by
                region name. Its keys are the regions that build - the workload's
                targets, since a region builds what it runs.

        Returns:
            The build plan.
        """
        ...

    def trigger(self, cluster: NamespacedCluster, name: str, group: str) -> bool:
        """Ask for one more build of inputs that have not changed.

        The counterpart to :meth:`plan`, and the only imperative call in this
        protocol: ``plan`` describes desired state, and re-applying a spec that
        already matches is a no-op no backend rebuilds from. Building the same
        source again - against today's base image and dependencies - is
        therefore a call of its own (docs/BUILDING.md - What causes a new Build).

        Call it *after* applying the plan, so a region that has no build objects
        gets them (and builds) first.

        Args:
            cluster: The cluster holding the build (always the local region).
            name: The workload name.
            group: The owning group.

        Returns:
            True if a build was triggered. False when there is nothing to
            trigger yet, which is not a failure: an Image with no build behind it
            is already about to produce one.
        """
        ...

    def status(self, cluster: NamespacedCluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...

    def statuses(self, cluster: NamespacedCluster, group: str) -> dict[str, BuildStatus]:
        """Every build state a group has on one cluster, keyed by workload object name.

        The listing counterpart of :meth:`status`: one label-selected read
        answers for every workload in the group, whatever their number.

        Args:
            cluster: The cluster to read (normally the local region).
            group: The owning group.

        Returns:
            ``{object_name: BuildStatus}``, omitting workloads with no build.
        """
        ...


def image_reference(registry_base: str, req: BuildRequest) -> str:
    """The image reference convention for a build: ``{base}/{group}/{name}:{tag}``.

    Shared so the API and the build backend agree on where a build's image lands.

    The tag is the branch projected into what an OCI tag allows - a branch may
    contain ``/`` and a tag may not, so ``feature/login`` pushes to
    ``feature-login``. Only the tag is rewritten; the build still compiles that
    exact ref (see ``BuildRequest.build_revision``).

    Args:
        registry_base: Registry host, plus organization when the registry has
            one (``RegistryConfig.base``).
        req: The build request.

    Returns:
        The fully-qualified image reference.
    """
    base = registry_base.rstrip("/")
    return f"{base}/{image_repository(req.group, req.name)}:{image_tag(req.branch)}"


def cache_reference(registry_base: str, req: BuildRequest) -> str:
    """Where a build's layer cache lives: ``{base}/{group}/{name}_cache:latest``.

    The registry form of kpack's cache (docs/BUILDING.md - Build cache).

    The ``_`` makes a collision with a function image impossible: a name is a
    DNS-1123 label, which admits only ``[a-z0-9-]``, so no function can be named
    ``{name}_cache``.

    Args:
        registry_base: Registry host, plus organization when the registry has
            one (``RegistryConfig.base``).
        req: The build request.

    Returns:
        The fully-qualified cache reference.
    """
    base = registry_base.rstrip("/")
    return f"{base}/{cache_repository(req.group, req.name)}:latest"
