"""The build domain shared by the API (client) and the build service.

Both sides import these types, so the request/response shape can't drift between
the caller and the backend. :class:`BuildBackend` is implemented today by the
in-process ``KpackBackend``; a remote build microservice would implement the
same protocol with no change to callers.

The protocol is named ``BuildBackend``, not ``Builder``: kpack has its own
``Builder`` CR (the buildpack composition an ``Image`` references by name, see
docs/BUILDING.md - Buildpack Topology), and one spelling cannot mean both.
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
class SiteBuild:
    """One site's half of a build plan.

    Attributes:
        tag: The image reference this site's build pushes to, in its own
            registry.
        manifests: Its ``Image`` and build ServiceAccount, in dependency order.
    """

    tag: str
    manifests: list[dict]


@dataclass
class BuildPlan:
    """What declaring a build produces, split by how far each piece travels.

    Attributes:
        replicated: Manifests every site needs. The git credential lives here:
            a site must be able to rebuild from a token it already holds, which
            is the switchover story (docs/BUILDING.md - Active/Active).
        per_site: The build objects each site applies, keyed by site name. Per
            site because each pushes to its own registry, so the tag differs;
            one shared tag would have two clusters racing to push it
            (docs/BUILDING.md - Registry layout).
    """

    replicated: list[dict]
    per_site: dict[str, SiteBuild]

    def tag_for(self, site: str) -> str | None:
        """The image reference ``site`` builds to, or None if it does not build.

        Args:
            site: The site name.

        Returns:
            The tag, or None when the plan does not cover that site.
        """
        build = self.per_site.get(site)
        return build.tag if build else None

    def manifests_for(self, site: str) -> list[dict]:
        """The build manifests ``site`` applies, empty if it does not build.

        Args:
            site: The site name.

        Returns:
            The manifests, in dependency order.
        """
        build = self.per_site.get(site)
        return build.manifests if build else []

    @property
    def tags(self) -> dict[str, str]:
        """The image reference each site builds to, keyed by site name."""
        return {site: build.tag for site, build in self.per_site.items()}

    @property
    def manifests_by_site(self) -> dict[str, list[dict]]:
        """Each site's build manifests, keyed by site name."""
        return {site: build.manifests for site, build in self.per_site.items()}


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
            registries: The registry each building site pushes to, keyed by
                site name. Its keys are the sites that build - the workload's
                targets, since a site builds what it runs.

        Returns:
            The build plan.
        """
        ...

    def trigger(self, cluster: Cluster, name: str, group: str) -> bool:
        """Ask for one more build of inputs that have not changed.

        The counterpart to :meth:`plan`, and the only imperative call in this
        protocol: ``plan`` describes desired state, and re-applying desired state
        that already matches is by design a no-op no backend rebuilds from. A
        rebuild request is not a state change - the caller is asking for the same
        source to be built again, against today's base image and dependencies -
        so it cannot be expressed as one without putting a nonce in the spec and
        rebuilding forever.

        Call it *after* applying the plan, so a site that has no build objects
        gets them (and builds) rather than being triggered into nothing.

        Args:
            cluster: The cluster holding the build (always the local site).
            name: The workload name.
            group: The owning group.

        Returns:
            True if a build was triggered. False when there is nothing to
            trigger yet, which is not a failure: an Image with no build behind it
            is already about to produce one.
        """
        ...

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...

    def statuses(self, cluster: Cluster, group: str) -> dict[str, BuildStatus]:
        """Every build state a group has on one cluster, keyed by workload object name.

        The listing counterpart of :meth:`status`. It exists as its own call
        rather than a loop over :meth:`status` because a listing needs the whole
        group at once: one label-selected read answers for every workload,
        where the loop would be a round trip per workload on every poll.

        Args:
            cluster: The cluster to read (normally the local site).
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
    return f"{base}/{cache_repository(req.group, req.name)}:latest"
