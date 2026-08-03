"""The build contract shared by the API (client) and the builder service.

Both sides import these types, so the request/response shape can't drift between
the caller and the builder. :class:`Builder` is implemented today by the
in-process ``KpackBuilder``; a remote builder microservice would implement the
same protocol with no change to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic.dataclasses import dataclass as validated_dataclass

from common.names import Branch, GitUrl, Group, Name, SourcePath, image_tag

if TYPE_CHECKING:  # a type hint only - importing it would pull the k8s client
    from common.cluster import Cluster


@validated_dataclass
class BuildRequest:
    """Inputs for building a function image from source.

    Validated on construction rather than trusted: these fields become
    Kubernetes object names and an image reference, and the build path is
    reachable off the HTTP edge (the webhook, and later the build service),
    where request-model validation has not run.

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
        revision: Exact commit to build. None means build the branch head,
            which is what create/update do; the webhook path will pin the
            pushed SHA here so a rebuild is idempotent.
    """

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
    """What declaring a build produces, split by how far each piece travels.

    Attributes:
        tag: The image reference the build pushes to.
        replicated: Manifests every site needs. The git credential lives here:
            a site that has never built the function still has to be able to,
            which is the whole switchover story (docs/BUILDING.md - Active/Active).
        local: Manifests for the one site that builds - the Image and its
            ServiceAccount. Replicating these would have every site build the
            same source and race to push the same tag (docs/BUILDING.md - Active/Active).
    """

    tag: str
    replicated: list[dict]
    local: list[dict]


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


class Builder(Protocol):
    """Declares function builds and reports their state."""

    @property
    def pull_secret(self) -> str:
        """The registry Secret a built function's KSVC pulls its image with."""
        ...

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference the build will push to (deterministic, no I/O)."""
        ...

    def plan(self, req: BuildRequest, labels: dict[str, str]) -> BuildPlan:
        """The manifests declaring the build, split by replication scope.

        Pure: returning does not mean an image exists, or even that anything has
        been applied. A backend that builds asynchronously (kpack) describes the
        desired state and lets the caller apply it as owned resources of the
        workload.

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.

        Returns:
            The build plan.
        """
        ...

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...


def image_reference(registry_base: str, req: BuildRequest) -> str:
    """The image reference convention for a build: ``{base}/{group}/{name}:{tag}``.

    Shared so the API and the builder agree on where a build's image lands.

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
    return f"{base}/{req.group}/{req.name}:{image_tag(req.branch)}"


def cache_reference(registry_base: str, req: BuildRequest) -> str:
    """Where a build's layer cache lives: ``{base}/{group}/{name}_cache:latest``.

    The registry form of kpack's cache, preferred over the volume form because
    that one is a PVC per function (docs/BUILDING.md - Build cache).

    The ``_`` is what makes a collision with a function image impossible rather
    than unlikely: a name is a DNS-1123 label, which admits only ``[a-z0-9-]``,
    so no function can ever be named ``{name}_cache``. A reserved *tag* in the
    function's own repository would not be safe the same way - a branch named
    ``cache`` projects to exactly that tag (:func:`common.names.image_tag`) - and
    neither would a nested ``{name}/cache`` path, which adds a repository level
    that Quay only accepts with extended repository names enabled.

    Args:
        registry_base: Registry host, plus organization when the registry has
            one (``RegistryConfig.base``).
        req: The build request.

    Returns:
        The fully-qualified cache reference.
    """
    base = registry_base.rstrip("/")
    return f"{base}/{req.group}/{req.name}_cache:latest"
