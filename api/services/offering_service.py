"""The per-offering service the routers depend on: the shared engine, one offering bound.

:class:`~api.services.workloads.WorkloadService` is offering-agnostic and takes
the :class:`~api.services.offering.Offering` on every call. Each router is
injected with a service for its offering instead (``FunctionDep``,
``ContainerDep``), and that service fixes the offering here, at class level.

The methods that behave identically for every offering - the reads, the
streams, the delete - are defined on this base. A subclass defines the ones
that differ (create, update, a function's build, a container's pull).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from cloudlet_apis.auth import Principal

from api.models.common import (
    PodLogSnapshot,
    PodRoster,
    WorkloadResponse,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.services.offering import Offering
from api.services.streams.sse import StreamEvent
from api.services.workloads import WorkloadService


class OfferingService:
    """The workload operations every offering shares, with :attr:`offering` bound.

    Each method runs the engine operation of the same name for this service's
    offering. A subclass sets :attr:`offering` and adds whatever its offering
    needs beyond these.
    """

    offering: ClassVar[Offering]

    def __init__(self, engine: WorkloadService):
        """Bind the shared engine.

        Args:
            engine: The shared workload engine doing the cross-region work.
        """
        self._engine = engine

    async def get(self, name: str, group: str, user: Principal) -> WorkloadResponse:
        """Read one workload with live per-region status and its redacted spec.

        Fans out to every region. A region that answers with a clean 404 is left
        out of the response; one that cannot answer at all stays visible as a
        failed row and degrades the rollup.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The full single-workload response.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If it cannot be confirmed absent because a
                region was unreachable.
        """
        return await self._engine.get(self.offering, name, user, group)

    async def stats(self, name: str, group: str, user: Principal) -> WorkloadStatsResponse:
        """Read the workload's live state: the rollup, and per-region replicas and usage.

        The poll counterpart to :meth:`get`, with none of the desired-state
        reads - no file ConfigMaps and no backing Secret - so a client
        refreshing every few seconds pulls no secret material out of the
        cluster on a loop.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The live stats view.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If it cannot be confirmed absent because a
                region was unreachable.
        """
        return await self._engine.stats(self.offering, name, user, group)

    async def pods(self, name: str, group: str, user: Principal) -> PodRoster:
        """List the workload's pods on the local region, read once.

        The non-streaming form of :meth:`stream_pods`, for a caller that cannot
        hold a connection open. Local region only, matching the log endpoints it
        feeds: a pod name is only useful where its log can be read.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The roster.

        Raises:
            NotFoundError: If the workload is not on the local region, or the
                caller cannot access it (hidden as a 404, matching the GET).
        """
        return await self._engine.pods(self.offering, name, user, group)

    async def pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
        tail_lines: int | None = None,
    ) -> PodLogSnapshot:
        """Read one of the workload's pods' logs as it stands, once.

        The non-streaming form of :meth:`stream_pod_logs`, through the same
        authorization. What comes back is bounded by what the node still holds
        and by the deployment's snapshot bounds; the caller's own bounds are
        clamped to those, never widened.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to read.
            container: The pod container to read.
            since_seconds: Only lines newer than this, if set.
            limit_bytes: Cap on the bytes read, if set; clamped to the
                configured ceiling.
            tail_lines: Newest lines wanted, if set; clamped to the configured
                snapshot bound.

        Returns:
            The snapshot.

        Raises:
            NotFoundError: If the workload or the pod is not here, the pod is
                not this workload's, or the caller cannot access it (all hidden
                as a 404).
        """
        return await self._engine.pod_logs(
            self.offering,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
            tail_lines=tail_lines,
        )

    async def stream_pods(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Stream the workload's pod roster on the local region, on an interval.

        The first roster is read before the stream opens, so a workload that
        does not exist is a 404 with an error envelope rather than a stream that
        opens and immediately fails.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between listings; None takes the configured
                default.

        Returns:
            The event stream, beginning with a ``pods`` event.

        Raises:
            NotFoundError: If the workload is not on the local region, or the
                caller cannot access it (hidden as a 404).
            ServiceUnavailableError: If no stream slot is free.
        """
        return await self._engine.stream_pods(self.offering, name, user, group, interval=interval)

    async def stream_pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        tail_lines: int | None = None,
    ) -> AsyncIterator[StreamEvent | str]:
        """Follow one of the workload's pods' logs, on the local region.

        Local region only: Kubernetes keeps no log buffer beyond the node that
        wrote it. The named pod must also carry this workload's service label,
        so owning the workload is not on its own enough to read a pod.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to follow.
            container: The pod container to read.
            since_seconds: How far back the log starts, if set.
            tail_lines: Start at the newest this-many lines instead, if set;
                clamped to the configured snapshot bound.

        Returns:
            The event stream, beginning with an ``open`` event.

        Raises:
            NotFoundError: If the workload or the pod is not here, the pod is
                not this workload's, or the caller cannot access it (all hidden
                as a 404).
            ServiceUnavailableError: If no stream slot is free.
        """
        return await self._engine.stream_pod_logs(
            self.offering,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            tail_lines=tail_lines,
        )

    async def stream_stats(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Push the workload's live state on an interval, as a stream of events.

        The streaming counterpart to :meth:`stats`, reporting the same body;
        only the transport differs. The first reading is taken before the stream
        opens, so a workload that does not exist is a 404 with an error
        envelope.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between readings; None takes the configured
                default.

        Returns:
            The event stream, beginning with a ``stats`` event.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If no stream slot is free, or the workload
                cannot be confirmed absent because a region was unreachable.
        """
        return await self._engine.stream_stats(self.offering, name, user, group, interval=interval)

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """Summarize every workload of this offering that the group owns.

        Merged best-effort across regions: a workload's ``regions`` lists only
        those that returned it, and an unreachable region is skipped rather than
        failing the listing.

        Args:
            group: The owning group.
            user: The authenticated caller.
            sort: Sort key, "name" or "createdAt".

        Returns:
            The per-workload summaries.

        Raises:
            RegionTotalFailure: If every region is unreachable.
        """
        return await self._engine.list(self.offering, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete the workload from every region; ownerReferences cascade the rest.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Raises:
            NotFoundError: If the workload exists on no region, or the caller
                cannot access it (hidden as a 404, matching the GET).
            ServiceUnavailableError: If a region could not be reached, so the
                delete cannot be confirmed.
        """
        await self._engine.delete(self.offering, name, user, group)
