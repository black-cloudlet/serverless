"""Everything one apply needs, as a value instead of a signature."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cloudlet_apis.auth import Principal
from cloudlet_apis.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ApplyRequest:
    """Everything one apply needs, as a value instead of a signature.

    Built by the offering service and handed to
    :meth:`WorkloadService.apply_workload`. The fields are the *union* of both
    offerings' needs: a container leaves the build metadata None, a function
    leaves the pull-secret manifest None.

    Attributes:
        name: Workload name.
        group: Owning group.
        user: The authenticated caller.
        image: The image to deploy when every region runs the same one (a
            container's). Empty for an offering built per region; ``images``
            carries it then.
        env: Env vars to resolve onto the workload.
        files: File mounts to resolve onto the workload.
        scaling: Autoscaling settings.
        size: Resource t-shirt size.
        hostname: Optional custom host; None takes the default.
        regions: Target region names, or None for all.
        port: The container port to stamp. Always set; both offerings default
            it to 8080.
        created: True for a create - enables the absence check and the
            rollback of a half-applied workload, and picks the success status.
        pull_secret_name: Name of the image-pull Secret the KSVC references.
        pull_secret_manifest: The pull Secret to apply, when this offering
            creates one (a container's; a function's is the chart's).
        prev_host: The host the workload currently uses (update only); when it
            differs from the resolved host, the old DomainMapping is retired so
            the old host doesn't stay claimed.
        kept_env: Decoded existing env-Secret values, so a secret env var sent
            without a value keeps its stored value (update only).
        kept_files: Decoded existing files-Secret values, so a secret file sent
            without content keeps its stored content (update only).
        images: The image to run, per region name; takes precedence over
            ``image``. A function builds in each region's own registry, so its
            regions do not run one reference.
        extra_secrets: Owned Secrets applied to every target region (the
            function's git token), so any of them can rebuild after a
            switchover. Not in the managed prune set, so omitting one keeps the
            stored copy.
        region_resources: Owned manifests applied to one region only, keyed by region
            name (a function's ``Image`` and build ServiceAccount). A region
            builds what it runs, so these go to the targets, and each is owned
            by the KSVC beside it.
        runtime: Function runtime, stamped as an annotation.
        version: Requested language version, stamped as an annotation. None
            means the caller took the platform default.
        git_url: Function source repo, stamped as an annotation.
        revision: Function source revision - branch, tag or commit - stamped
            as an annotation.
        path: Function source sub-directory, stamped as an annotation.
        pull_stamp: The workload's current pull stamp, carried forward so a
            re-composed spec does not drop it and cut a revision.
    """

    name: str
    group: str
    user: Principal
    image: str
    env: list
    files: list
    scaling: object
    size: str
    hostname: str | None
    regions: list[str] | None
    port: int
    created: bool
    pull_secret_name: str | None = None
    pull_secret_manifest: dict | None = None
    prev_host: str | None = None
    kept_env: dict[str, str] | None = None
    kept_files: dict[str, bytes] | None = None
    images: Mapping[str, str] = field(default_factory=dict)
    extra_secrets: Sequence[dict] = field(default_factory=tuple)
    region_resources: Mapping[str, Sequence[dict]] = field(default_factory=dict)
    # Build metadata, stamped as KSVC annotations so a read can report the source
    # a function was built from. All None for an offering that has no build.
    runtime: str | None = None
    version: str | None = None
    git_url: str | None = None
    revision: str | None = None
    path: str | None = None
    pull_stamp: str | None = None

    def image_for(self, region: str) -> str:
        """The image ``region`` should run.

        Args:
            region: The region name.

        Returns:
            That region's own image, falling back to the uniform ``image``.
        """
        return self.images.get(region) or self.image
