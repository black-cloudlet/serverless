"""Shared workload engine: build manifests once, fan out to all regions.

One class, in service.py - the orchestration reads front to back in one file.
Only the self-contained values live beside it: request.py (ApplyRequest) and
stream_guard.py (tying a stream's admission slot to its generator). The public
names are re-exported here, so ``from api.services.workloads import ...`` is
unchanged.
"""

from api.services.workloads.request import ApplyRequest
from api.services.workloads.service import WorkloadService

__all__ = ["ApplyRequest", "WorkloadService"]
