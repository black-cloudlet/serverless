"""Shared workload engine: build manifests once, fan out to all regions.

The orchestration is :class:`~api.services.workloads.service.WorkloadService` in
service.py. The values it works with sit beside it: request.py holds
:class:`ApplyRequest`, stream_guard.py ties a stream's admission slot to its
generator. Both public names are re-exported here, so
``from api.services.workloads import ApplyRequest, WorkloadService`` resolves.
"""

from api.services.workloads.request import ApplyRequest
from api.services.workloads.service import WorkloadService

__all__ = ["ApplyRequest", "WorkloadService"]
