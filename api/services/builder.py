"""Function builds, driven by kpack ``Image`` CRs (docs/BUILD-PIPELINE.md §8).

The API does not run builds; it declares them. Each create/update server-side
applies the function's git Secret, build ServiceAccount and ``Image`` to the
LOCAL cluster's build namespace, and kpack does the rest: clone, detect, build
with Cloud Native Buildpacks, push to the internal registry.

That makes ``build`` return as soon as the desired state is recorded, not when
an image exists. Callers deploy the KSVC against the deterministic tag and read
progress back through :meth:`KpackBuilder.status` - the reason a just-created
function reports ``Building`` rather than ``Ready`` (§10).

Applying rather than creating is also what makes this safe under active/active:
the manifests are a pure function of the request, so concurrent writes from two
API instances converge, and a site that has never built this function
reconstructs everything it needs on the next write (§9).
"""

from __future__ import annotations

from api.services import kpack
from api.services.deployer import Deployer
from api.services.runtimes import RuntimeRegistry
from common.cluster import Cluster, ResourceKind
from common.config import CommonSettings
from common.contract import BuildRequest, BuildResult, BuildStatus, image_reference
from common.errors import NotFoundError, ValidationError
from common.labels import workload_labels
from common.logging import get_logger

logger = get_logger(__name__)


class KpackBuilder:
    """Declares function builds as kpack Images on the local cluster."""

    def __init__(
        self,
        settings: CommonSettings,
        deployer: Deployer,
        runtimes: RuntimeRegistry,
    ):
        """Initialize the builder.

        Args:
            settings: Shared settings (registry, build namespace, credentials).
            deployer: Supplies the local cluster - builds are local-site only.
            runtimes: Resolves a runtime to its Builder and build environment.
        """
        self._settings = settings
        self._registry = settings.registry
        self._build = settings.build
        self._deployer = deployer
        self._runtimes = runtimes

    @property
    def _namespace(self) -> str:
        """The namespace build objects are created in."""
        return self._settings.build_namespace

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference for a build (see ``common.contract.image_reference``).

        Args:
            req: The build request.

        Returns:
            The fully-qualified image reference.
        """
        return image_reference(self._registry.base, req)

    def _runtime_config(self, runtime: str) -> tuple[str, list[dict], dict]:
        """Resolve a runtime to ``(builder, build_env, resources)``.

        The runtimes file is rendered by the chart with each entry's build
        environment already merged (shared env, dependency mirror, per-runtime
        overrides), so nothing is composed here beyond adding the version.

        Args:
            runtime: The requested runtime name.

        Returns:
            The Builder name, the build env list, and the build pod resources.

        Raises:
            ValidationError: If the runtime is unknown or names no Builder.
        """
        spec = self._runtimes.get(runtime)
        if spec is None:
            available = ", ".join(self._runtimes.names())
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )
        extra = spec.model_dump()
        builder = extra.get("builder")
        if not builder:
            raise ValidationError(
                f"runtime '{runtime}' has no `builder`; the runtimes ConfigMap must map "
                "every runtime to a kpack Builder before functions can be built."
            )
        env = [dict(e) for e in (extra.get("buildEnv") or [])]
        # Pin the language version the same way the chart documents it: the
        # runtime names the env var, so a version bump is a ConfigMap edit.
        version_env, version = extra.get("versionEnv"), extra.get("defaultVersion")
        if version_env and version and not any(e.get("name") == version_env for e in env):
            env.append({"name": version_env, "value": str(version)})
        resources = extra.get("buildResources") or self._build.resources
        return builder, env, dict(resources or {})

    def build(self, req: BuildRequest) -> BuildResult:
        """Declare the build: apply the git Secret, ServiceAccount and Image.

        Blocking (three cluster calls); callers run it off the event loop.

        Args:
            req: The build request.

        Returns:
            The image reference the build pushes to. No digest - the build has
            only been requested, not finished.

        Raises:
            ValidationError: If the runtime is unknown or maps to no Builder.
        """
        oname = f"{req.name}-{req.group}"
        builder, env, resources = self._runtime_config(req.runtime)
        tag = self.image_ref(req)
        labels = workload_labels(req.group, req.owner, oname, "function")
        cluster = self._deployer.local_cluster()

        secret_name = kpack.git_secret_name(oname)
        account_name = kpack.build_object_name(oname)

        # Order matters: the ServiceAccount references the Secret, and the Image
        # references the ServiceAccount. kpack tolerates a dangling reference by
        # waiting, but applying in dependency order means a build never starts
        # against half-built credentials.
        cluster.apply(
            kpack.build_git_secret(
                secret_name, labels, req.git_url, self._build.git_username, req.git_token
            ),
            namespace=self._namespace,
        )
        cluster.apply(
            kpack.build_service_account(
                account_name, labels, secret_name, self._build.registry_secret
            ),
            namespace=self._namespace,
        )
        image = kpack.build_image(
            account_name,
            labels,
            tag=tag,
            builder=builder,
            service_account=account_name,
            git_url=req.git_url,
            revision=req.build_revision,
            env=env,
            resources=resources,
        )
        cluster.apply(image, namespace=self._namespace)
        logger.info(
            "kpack image applied: name=%s site=%s runtime=%s builder=%s git=%s@%s -> %s",
            account_name,
            cluster.site,
            req.runtime,
            builder,
            req.git_url,
            req.build_revision,
            tag,
        )
        return BuildResult(image=tag)

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """Read a function's build state from one cluster.

        Args:
            cluster: The cluster to read (normally the local site).
            name: The workload name.
            group: The owning group.

        Returns:
            The build status, or None when the function has no Image on this
            cluster - which is the normal case for a site that has never built
            it, and must fall through to the KSVC status rather than read as a
            failure (§10).
        """
        object_name = kpack.build_object_name(f"{name}-{group}")
        try:
            image = cluster.get(ResourceKind.KPACK_IMAGE, object_name, namespace=self._namespace)
        except NotFoundError:
            return None
        except Exception:  # noqa: BLE001 - kpack absent or unreadable is not fatal
            logger.warning("could not read kpack Image '%s' on %s", object_name, cluster.site)
            return None
        state, latest, message = kpack.build_status(image)
        return BuildStatus(state=state, image=latest, message=message)

    def cleanup(self, cluster: Cluster, name: str, group: str) -> None:
        """Delete a function's build objects from one cluster.

        Best-effort and idempotent: an already-absent object is not an error.
        The Image must go or kpack keeps rebuilding the function forever after
        it is deleted (§11); the ServiceAccount and Secret would otherwise be
        left holding a git token.

        Args:
            cluster: The cluster to clean.
            name: The workload name.
            group: The owning group.
        """
        oname = f"{name}-{group}"
        targets = [
            (ResourceKind.KPACK_IMAGE, kpack.build_object_name(oname)),
            (ResourceKind.SERVICE_ACCOUNT, kpack.build_object_name(oname)),
            (ResourceKind.SECRET, kpack.git_secret_name(oname)),
        ]
        for kind, object_name in targets:
            try:
                cluster.delete(kind, object_name, namespace=self._namespace)
            except NotFoundError:
                continue
            except Exception:  # noqa: BLE001 - cleanup must not fail the delete
                logger.warning(
                    "could not delete %s '%s' on %s", kind.kind, object_name, cluster.site
                )
