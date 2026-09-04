"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

import functools
import hmac
from collections.abc import Mapping

from cloudlet_apis.auth import Principal
from cloudlet_apis.errors import UnauthenticatedError
from cloudlet_apis.logging import get_logger
from pydantic import ValidationError as PydanticValidationError

from api.core.paths import webhook_url
from api.models.function import (
    FunctionCreate,
    FunctionResponse,
    FunctionUpdate,
    WebhookView,
)
from api.models.webhook import GITLAB_PUSH_EVENT, GitLabPushEvent, WebhookOutcome
from api.services.builder.runtimes import RuntimeRegistry
from api.services.manifests import secrets as secret_svc
from api.services.offering import FUNCTION
from api.services.offering_service import OfferingService
from api.services.state import describe as describe_svc
from api.services.workloads import ApplyRequest, WorkloadService
from api.services.workloads.service import run_background
from common.build import BuildPlan, BuildRequest
from common.config import RegistryConfig
from common.errors import NotFoundError, ValidationError
from common.labels import OFFERING_FUNCTION, workload_labels
from common.names import same_repository

logger = get_logger(__name__)


class FunctionService(OfferingService):
    """Function orchestration: the runtime check and the build, over the engine.

    Create and update validate the runtime, plan the kpack build and hand the
    engine an :class:`ApplyRequest` carrying the build objects; rebuild plans
    the build alone from the stored inputs. Reads, streams and delete are
    :class:`OfferingService`'s.
    """

    offering = FUNCTION

    def __init__(self, engine: WorkloadService, runtimes: RuntimeRegistry):
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-region work.
            runtimes: The available-runtimes registry, supplied by the DI layer
                (``api.dependencies.get_function_service``).
        """
        super().__init__(engine)
        self._runtimes = runtimes

    def _assert_runtime(self, runtime: str, version: str | None = None) -> None:
        """Reject an unknown/unbuildable runtime or version (400, before the 202).

        Checks that the runtime is in the mounted ConfigMap and that it maps to
        a kpack Builder, and the version against the same ``versions`` list
        ``/info`` advertises (docs/FUNCTIONS.md - Overview).

        Args:
            runtime: The requested runtime.
            version: The requested language version, or None for the default.

        Raises:
            ValidationError: If ``runtime`` isn't available, names no Builder, or
                ``version`` isn't one the runtime offers.
        """
        spec = self._runtimes.get(runtime)
        if spec is None:
            available = ", ".join(self._runtimes.names()) or "none configured"
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )
        if not spec.builder:
            raise ValidationError(
                f"runtime '{runtime}' is not buildable: it maps to no kpack ClusterBuilder. "
                "The runtimes ConfigMap is missing or incomplete."
            )
        if version is None:
            return
        # No versionEnv means there is no build variable to set; an empty
        # `versions` means the runtime pins its own. Neither is selectable.
        if not spec.versionEnv or not spec.versions:
            raise ValidationError(
                f"runtime '{runtime}' does not offer a choice of version; omit 'version'"
            )
        if version not in spec.versions:
            raise ValidationError(
                f"unsupported version '{version}' for runtime '{runtime}'; "
                f"available versions: {', '.join(spec.versions)}"
            )

    def _plan(
        self, req: BuildRequest, user: Principal, registries: Mapping[str, RegistryConfig]
    ) -> BuildPlan:
        """The owned manifests that declare the build, and the tags they push to.

        Includes the workload's ``{workload}-git`` Secret: one Secret serves both
        the API (reading the token back on a later edit) and kpack (cloning with
        it), because the build runs in the workload's own namespace.

        Args:
            req: The build request.
            user: The authenticated caller, for the ownership labels.
            registries: The registry each targeted region builds into.

        Returns:
            The build plan, one tag and one set of manifests per region.
        """
        labels = workload_labels(req.group, user.username, req.name, OFFERING_FUNCTION)
        return self._engine.builder.plan(req, labels, registries)

    # Validate synchronously for an immediate 400/404/409, then build and deploy
    # in the background behind a 202 (docs/API.md - Request semantics).
    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets/gitToken never echoed)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            runtime=spec.runtime,
            version=spec.version,
            gitRepo=spec.gitRepo,
            revision=spec.revision,
            path=spec.path,
            port=spec.port,
        )

    async def accept(
        self, group: str, spec: FunctionCreate, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept a create request, scheduling the build+deploy (202).

        Args:
            group: The owning group (from the request path).
            spec: The function create request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the build+deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self._assert_runtime(spec.runtime, spec.version)
        # Minted here rather than in the background work, so the 202 can carry it:
        # a caller configures the git hook from the create response and never has
        # to come back for it (docs/FUNCTIONS.md - Git webhook).
        token = secret_svc.new_webhook_token()
        # No name check beyond the engine's shared one: the kpack Image is the
        # workload's own name verbatim, so the 63-character label
        # value it must fit is exactly the limit the engine already enforces
        # (common/kpack.py - build_image_name).
        return await self._engine.accept_create(
            offering=FUNCTION,
            group=group,
            spec=spec,
            user=user,
            background=background,
            work=functools.partial(self.create, webhook_token=token),
            webhook=self._webhook_view(group, spec.name, token),
            **self._echo(spec),
        )

    def _webhook_view(self, group: str, name: str, token: str) -> WebhookView:
        """What a caller needs to configure a push to build this function."""
        return WebhookView(
            url=webhook_url(self._engine.settings, group, name),
            token=token,
        )

    def _webhook_secret(
        self, group: str, name: str, user: Principal, token: str | None = None
    ) -> dict:
        """The Secret holding this function's webhook token, for every region.

        Travels with the git Secret through ``extra_secrets``, so it is applied
        wherever the workload runs and is never in the managed prune set - a
        write that does not carry it keeps the stored copy.

        Args:
            group: The owning group.
            name: The workload name.
            user: The caller, for the ownership labels.
            token: The token to store; None mints a fresh one.

        Returns:
            The Secret manifest.
        """
        labels = workload_labels(group, user.username, name, OFFERING_FUNCTION)
        return secret_svc.build_webhook_secret(
            secret_svc.webhook_secret_name(name),
            labels,
            token or secret_svc.new_webhook_token(),
        )

    async def accept_update(
        self, group: str, name: str, spec: FunctionUpdate, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept an update request, scheduling the deploy (202).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The function update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self._assert_runtime(spec.runtime, spec.version)
        return await self._engine.accept_update(
            offering=FUNCTION,
            group=group,
            name=name,
            spec=spec,
            user=user,
            background=background,
            work=self.update,
            **self._echo(spec),
        )

    def _build_request(
        self,
        name: str,
        group: str,
        existing: dict,
        user: Principal | None = None,
        *,
        commit: str | None = None,
    ) -> BuildRequest:
        """Reconstruct the build inputs of a deployed function, for a rebuild.

        Every input comes back off the workload itself - the KSVC's annotations
        and the ``{workload}-git`` Secret - which is the same reconstruction a
        region that has never built the function does after a switchover
        (docs/BUILDING.md - Reconstruction after a gap). Nothing is taken
        from the request (docs/FUNCTIONS.md - Building again without changing
        anything).

        Args:
            name: The workload name.
            group: The owning group.
            existing: The state loaded by ``load_existing``.
            user: The authenticated caller, stamped as the build's owner. None
                for a build no caller authenticated - a webhook push - which
                takes the function's own owner off its ownership label instead.
            commit: The commit a push delivered, built instead of the revision's
                head. None follows the revision, which is what create, update
                and the explicit rebuild do; a stored pin is deliberately *not*
                carried in here, so a rebuild returns to the head
                (docs/FUNCTIONS.md - Git webhook).

        Returns:
            The build request.

        Raises:
            ValidationError: If the stored state cannot describe a build - no
                token, or missing build metadata.
        """
        git_url = existing.get("gitUrl")
        revision = existing.get("revision")
        runtime = existing.get("runtime")
        missing = [
            field
            for field, value in (("gitRepo", git_url), ("revision", revision), ("runtime", runtime))
            if not value
        ]
        if missing:
            raise ValidationError(
                f"cannot rebuild: the function has no stored {', '.join(missing)}; "
                "send the build inputs with a PUT instead"
            )
        token = existing.get("git_token")
        if not token:
            raise ValidationError(
                "cannot rebuild: no git token is stored for this function; "
                "send one with a PUT instead"
            )
        # Built from stored state rather than from a validated request body, so
        # the model's own validation can fail here; it is translated into a 400.
        try:
            return BuildRequest(
                name=name,
                group=group,
                git_url=git_url,
                revision=revision,
                path=existing.get("path") or "",
                git_token=token,
                runtime=runtime,
                version=existing.get("version"),
                owner=user.username if user else existing.get("owner", ""),
                commit=commit,
            )
        except PydanticValidationError as exc:
            fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")}))
            raise ValidationError(
                f"cannot rebuild: the stored build inputs are not valid ({fields}); "
                "send them with a PUT instead"
            ) from exc

    async def accept_build(
        self, group: str, name: str, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept a rebuild request, scheduling the build (202).

        Loads and authorizes the function synchronously, so a missing workload,
        a missing token or a runtime no longer in the ConfigMap is an immediate
        404/400 (docs/FUNCTIONS.md - Building again without changing anything).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the build on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.

        Raises:
            NotFoundError: If no such function exists (or it isn't the caller's).
            ValidationError: If the stored state cannot describe a build.
        """
        existing = await self._engine.load_existing(name, FUNCTION, user, group)
        req = self._build_request(name, group, existing, user)
        # A runtime can be removed from the ConfigMap after a function was built
        # with it; that is a 400 here, before the 202.
        self._assert_runtime(req.runtime, req.version)
        background.add_task(run_background, self.build, group, name, user, existing)
        host = existing.get("host") or self._engine.host_for(name, None, group)
        return self._engine.accepted(
            FUNCTION,
            name,
            group,
            host,
            runtime=req.runtime,
            version=req.version,
            gitRepo=req.git_url,
            revision=req.revision,
            path=req.path,
        )

    async def build(self, group: str, name: str, user: Principal, existing: dict) -> None:
        """Build a function's current source again (runs in the background).

        The image is rebuilt from the same repository, revision and runtime it
        already has, so this is what picks up a base-image or dependency change,
        retries a failed build, or gets a new commit built now rather than when
        kpack next polls. The running revision keeps serving until the new digest
        is rolled out (docs/BUILDING.md - Ownership: API vs Build Service).

        It also **returns the function to its revision's head**: a commit a push
        pinned is cleared, so what is built is what the caller asked for rather
        than wherever the last push left it. That clearing is itself the spec
        change kpack builds from, which is why the trigger is only sent when
        there was no pin - with one, a trigger would produce a second build
        (docs/FUNCTIONS.md - Git webhook).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.
            existing: The workload state preloaded (and authorized) by
                :meth:`accept_build`.

        Raises:
            ValidationError: If the stored state cannot describe a build.
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        req = self._build_request(name, group, existing, user)
        # Every configured region; apply_build skips the ones not running it.
        registries = self._engine.target_registries()
        had_commit = existing.get("commit") is not None
        await self._engine.apply_build(
            name,
            group,
            self._plan(req, user, registries),
            commit=None,
            had_commit=had_commit,
            trigger=not had_commit,
        )

    # ------------------------------------------------------------------ webhook

    def _webhook_principal(self, group: str) -> Principal:
        """The identity a push acts as: this group, no admin, no user.

        A push is authenticated by a token, not by SSO, so there is no caller to
        authorize. Naming a principal anyway is what keeps the tenancy check on
        the one path that has no bearer: the load goes through the same
        ``load_existing`` every other read uses, and a function in another group
        is invisible to it.

        Args:
            group: The group whose function the delivery names.

        Returns:
            A non-admin principal scoped to that group alone.
        """
        return Principal(subject=f"webhook:{group}", username="webhook", groups=[group])

    @staticmethod
    def _ignored(event: GitLabPushEvent | None, kind: str | None, existing: dict) -> str | None:
        """Why this delivery starts no build, or None if it should.

        Every answer here is a ``200``: GitLab disables a hook that keeps
        returning ``4xx``, so a push this function does not care about must read
        as success (docs/FUNCTIONS.md - Git webhook).

        Args:
            event: The parsed push payload, or None if the body was empty.
            kind: The ``X-Gitlab-Event`` header value.
            existing: The function's stored state.

        Returns:
            A human-readable reason, or None to build.
        """
        if kind and kind != GITLAB_PUSH_EVENT:
            return f"event '{kind}' is not handled; configure the hook for push events"
        if event is None or not event.is_push:
            return "body is not a push event"
        if event.is_deletion:
            return "the ref was deleted, so there is no commit to build"
        branch = event.branch
        if branch is None:
            return f"ref '{event.ref}' is not a branch"
        revision = existing.get("revision")
        if branch != revision:
            # Not an error, and the common case in a repository several
            # functions build from. A function whose revision is a tag or a
            # commit lands here for every push, which is what pinning meant.
            return f"pushed branch '{branch}' is not this function's revision '{revision}'"
        if not same_repository(event.project.git_http_url, existing.get("gitUrl") or ""):
            return "the push is from a different repository"
        if event.sha is None:
            return "the payload carries no usable commit"
        return None

    async def accept_webhook(
        self,
        group: str,
        name: str,
        token: str,
        event: GitLabPushEvent | None,
        event_kind: str | None,
        background,
    ) -> FunctionResponse | WebhookOutcome:
        """Authenticate a git push and, if it is this function's, build its commit.

        The unauthenticated half of ``POST .../build``. Everything before the
        token matches answers ``401``, including a function that does not exist:
        the caller has proved nothing yet, so it must not learn what is here.

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            token: The token the provider sent.
            event: The parsed push payload, or None if there was no body.
            event_kind: The ``X-Gitlab-Event`` header value, if any.
            background: FastAPI background tasks to schedule the build on.

        Returns:
            A Pending 202 body when a build was scheduled, else the outcome
            explaining why the delivery was ignored.

        Raises:
            UnauthenticatedError: If the token is missing, wrong, or there is no
                such function.
            ValidationError: If the stored state cannot describe a build.
        """
        principal = self._webhook_principal(group)
        try:
            existing = await self._engine.load_existing(name, FUNCTION, principal, group)
        except NotFoundError as exc:
            # Absent and not-yours are the same answer here, and neither is a
            # 404: the caller is unauthenticated until the token matches.
            raise UnauthenticatedError("invalid webhook token") from exc

        stored = existing.get("webhook_token")
        if not stored or not hmac.compare_digest(stored, token):
            raise UnauthenticatedError("invalid webhook token")

        reason = self._ignored(event, event_kind, existing)
        if reason is not None:
            logger.info("webhook: %s/%s: ignored: %s", group, name, reason)
            return WebhookOutcome(accepted=False, reason=reason)

        commit = event.sha
        req = self._build_request(name, group, existing, commit=commit)
        # A runtime removed from the ConfigMap since the function was built is a
        # 400 here, before the 202, exactly as on the bearer path.
        self._assert_runtime(req.runtime, req.version)
        logger.info("webhook: %s/%s: building commit %s", group, name, commit)
        background.add_task(run_background, self.build_at_commit, group, name, existing, req)
        host = existing.get("host") or self._engine.host_for(name, None, group)
        return self._engine.accepted(
            FUNCTION,
            name,
            group,
            host,
            runtime=req.runtime,
            version=req.version,
            gitRepo=req.git_url,
            # What the caller chose, not what was pushed: a push moves the commit
            # under the revision, never the revision itself.
            revision=req.revision,
            commit=commit,
            path=req.path,
        )

    async def build_at_commit(
        self, group: str, name: str, existing: dict, req: BuildRequest
    ) -> None:
        """Build one pushed commit, in every region (runs in the background).

        The commit is written into the ``Image``'s git revision and stamped on
        the workload, and the image *tag* is left alone - it follows the
        function's revision, so a push moves the digest that tag points at
        rather than the tag itself, and the Image is never recreated.

        No trigger: the changed revision is itself the spec change kpack builds
        from, so one push produces one build however many API replicas or
        provider retries handled it (docs/BUILDING.md - Convergence rules).

        Args:
            group: The owning group.
            name: The workload name.
            existing: The state loaded and authorized by :meth:`accept_webhook`.
            req: The build request, already carrying the pushed commit.

        Raises:
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        registries = self._engine.target_registries()
        await self._engine.apply_build(
            name,
            group,
            self._plan_for_owner(req, existing, registries),
            commit=req.commit,
            trigger=False,
        )

    def _plan_for_owner(
        self, req: BuildRequest, existing: dict, registries: Mapping[str, RegistryConfig]
    ) -> BuildPlan:
        """The build plan, labelled with the function's own owner.

        The bearer paths label a build with the caller who asked for it. A push
        has no caller, so the labels come off the workload instead - what
        ``existing`` read from its ownership label - and the build objects stay
        selectable exactly as every other copy of them is.

        Args:
            req: The build request.
            existing: The function's stored state, carrying its owner.
            registries: The registry each region builds into.

        Returns:
            The build plan.
        """
        labels = workload_labels(req.group, existing.get("owner", ""), req.name, OFFERING_FUNCTION)
        return self._engine.builder.plan(req, labels, registries)

    async def rotate_webhook(self, group: str, name: str, user: Principal) -> WebhookView:
        """Replace this function's webhook token, in every region it runs in.

        Not a 202: one small Secret is written and the caller needs the new
        token in the response to reconfigure the hook with. The old token stops
        working as each region's write lands; there is no overlap window,
        because a hook is reconfigured in seconds and a leaked token should not
        outlive the request that replaced it.

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.

        Returns:
            The new webhook configuration.

        Raises:
            NotFoundError: If no such function exists (or it isn't the caller's).
        """
        await self._engine.load_existing(name, FUNCTION, user, group)
        token = secret_svc.new_webhook_token()
        await self._engine.apply_owned_secret(
            name, group, self._webhook_secret(group, name, user, token)
        )
        logger.info("webhook: %s/%s: token rotated", group, name)
        return self._webhook_view(group, name, token)

    async def create(
        self,
        group: str,
        spec: FunctionCreate,
        user: Principal,
        webhook_token: str | None = None,
    ) -> tuple[FunctionResponse, int]:
        """Build from Git and deploy a new function (runs in the background).

        Args:
            group: The owning group (from the request path).
            spec: The function create request.
            user: The authenticated caller.
            webhook_token: The token minted by :meth:`accept` and already
                returned to the caller, so the Secret written here is the one
                they were told to configure. None mints a fresh one, which is
                only reachable if this is called outside the accept path.

        Returns:
            The response body and HTTP status code.

        Raises:
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        # A region builds what it runs, so the plan covers exactly the targets
        # (docs/BUILDING.md - A region builds what it runs).
        registries = self._engine.target_registries()
        plan = self._plan(
            BuildRequest(
                name=spec.name,
                group=group,
                git_url=spec.gitRepo,
                revision=spec.revision,
                path=spec.path,
                git_token=spec.gitToken,
                runtime=spec.runtime,
                version=spec.version,
                owner=user.username,
            ),
            user,
            registries,
        )

        # No absence probe here: apply_workload runs one combined host+absence
        # pass over the same targets immediately before it mutates.
        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=spec.name,
                user=user,
                group=group,
                # No single image: each region deploys at the tag its own build
                # pushes to, and reads Building until something lands there.
                image="",
                images=plan.tags,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                port=spec.port,
                # Pulled with the same credential kpack pushed with. The Secret is the
                # chart's, shared by every function, so it is referenced, never applied.
                pull_secret_name=self._engine.builder.pull_secret,
                created=True,
                runtime=spec.runtime,
                version=spec.version,
                git_url=spec.gitRepo,
                revision=spec.revision,
                path=spec.path,
                # The git and webhook credentials go to every region so any of
                # them can rebuild and authenticate a push; each region gets its
                # own Image (docs/BUILDING.md - Active/Active Behaviour).
                extra_secrets=[
                    *plan.replicated,
                    self._webhook_secret(group, spec.name, user, webhook_token),
                ],
                region_resources=plan.manifests_by_region,
            ),
            FUNCTION,
        )
        return body, code

    @staticmethod
    def _assert_rebuildable(spec: FunctionUpdate, existing: dict, token: str | None) -> None:
        """Refuse an update that changes a build input with no token to build it.

        This only refuses an untokenised rebuild; what rebuilds is kpack's own
        diff of the Image spec. ``version`` counts as a build input like
        ``revision`` and ``runtime``: it is replaced, not kept, so omitting it
        returns the function to the platform default and that is a change
        (docs/API.md - Request semantics).

        Args:
            spec: The update request.
            existing: The stored state the request replaces.
            token: The token the rebuild would use - the request's, else the
                stored one - or None when there is neither.

        Raises:
            ValidationError: If a build input changed and there is no token.
        """
        changed = (
            spec.gitRepo != existing.get("gitUrl")
            or spec.revision != existing.get("revision")
            or spec.path != (existing.get("path") or "")
            or spec.runtime != existing.get("runtime")
            or spec.version != existing.get("version")
        )
        if changed and token is None:
            raise ValidationError(
                "a git token is required to rebuild; none was supplied and none is stored"
            )

    def _plan_update(
        self,
        name: str,
        group: str,
        spec: FunctionUpdate,
        token: str,
        user: Principal,
        registries: Mapping[str, RegistryConfig],
    ) -> BuildPlan:
        """Declare the build an update's inputs describe, in every region.

        Emitted on EVERY update that has a token. Re-applying an unchanged spec
        is a no-op kpack does not rebuild from, but it recreates a missing
        Image after a switchover.

        Args:
            name: The workload name.
            group: The owning group.
            spec: The update request, carrying the build inputs.
            token: The git token to build with.
            user: The authenticated caller, stamped as the build's owner.
            registries: The registry each region builds into.

        Returns:
            The build plan.
        """
        return self._plan(
            BuildRequest(
                name=name,
                group=group,
                git_url=spec.gitRepo,
                revision=spec.revision,
                path=spec.path,
                git_token=token,
                runtime=spec.runtime,
                version=spec.version,
                owner=user.username,
            ),
            user,
            registries,
        )

    async def update(
        self,
        group: str,
        name: str,
        spec: FunctionUpdate,
        user: Principal,
        existing: dict | None = None,
    ) -> tuple[FunctionResponse, int]:
        """Apply an update to a function, rebuilding on a build-input or token change.

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The function update request.
            user: The authenticated caller.
            existing: The workload state preloaded by accept_update, if any; a
                fresh fetch is done when None.

        Returns:
            The response body and HTTP status code.

        Raises:
            ValidationError: If a rebuild is needed but no token was supplied and
                none is stored.
            ServiceUnavailableError: If a rebuild is requested but the build
                pipeline is unavailable.
        """
        # Reuse the load_existing result from accept_update (already authorized) to
        # avoid a second multi-region fanout; fall back to a fresh fetch otherwise.
        if existing is None:
            existing = await self._engine.load_existing(name, FUNCTION, user, group)

        # Full replace, so the build inputs are the request's. The token is the
        # redacted keep: the stored one is reused unless the client sent a new one.
        token = spec.gitToken or existing.get("git_token")
        self._assert_rebuildable(spec, existing, token)

        # The image is never rewritten here, whatever changed. After the create,
        # it is the controller's alone (docs/BUILD-CONTROLLER.md - Digest propagation):
        # the build this update declares has not pushed yet, so anything written
        # now is a revision of the code already running. Kept per region, since
        # each region runs what its own build pushed.
        # A PUT is a full replace of the desired spec, and a pinned commit is no
        # part of any spec a caller sent - it is a fact a push left behind. So an
        # update returns the function to its revision's head, like the explicit
        # rebuild, leaving a push the only thing that pins (docs/FUNCTIONS.md -
        # Git webhook). Cleared before the apply, so the KSVC leads.
        if existing.get("commit") is not None:
            await self._engine.clear_commit(name, group)
        images = dict(existing.get("images") or {})
        registries = self._engine.target_registries()
        plan = None
        if token is not None:
            plan = self._plan_update(name, group, spec, token, user, registries)
            # A region not running it yet deploys at its own tag and reads
            # Building until its first build lands, exactly as a create does.
            for region, tag in plan.tags.items():
                images.setdefault(region, tag)

        return await self._engine.apply_workload(
            ApplyRequest(
                name=name,
                user=user,
                group=group,
                image=existing["image"],
                images=images,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                # Replaced like every other non-secret field: omitting it returns
                # the function to 8080, as omitting `version` returns it to the
                # platform's default runtime version.
                port=spec.port,
                pull_secret_name=self._engine.builder.pull_secret,
                created=False,
                # stamp the (possibly updated) build metadata; never the token
                runtime=spec.runtime,
                version=spec.version,
                git_url=spec.gitRepo,
                revision=spec.revision,
                path=spec.path,
                prev_host=existing.get("host"),
                kept_env=existing.get("env_values"),
                kept_files=existing.get("files_values"),
                # Re-emitted on every update, like the git Secret: an update
                # targets every configured region, so a region that gains the
                # workload here has to gain the token with it. The stored value
                # is reused; minting is only reachable for a function that
                # somehow has none.
                extra_secrets=[
                    *(plan.replicated if plan else []),
                    self._webhook_secret(group, name, user, existing.get("webhook_token")),
                ],
                region_resources=plan.manifests_by_region if plan else {},
            ),
            FUNCTION,
        )
