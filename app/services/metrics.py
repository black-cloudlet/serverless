"""Parse and aggregate live resource usage from the metrics API (PodMetrics).

``metrics.k8s.io`` reports per-container usage as Kubernetes *quantity* strings
(e.g. cpu ``"1234567n"``, memory ``"123456Ki"``). We sum across all containers
of all the workload's pods into one cpu/memory figure per site.
"""

from __future__ import annotations

import re

from app.models.common import ResourceUsage

_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)([a-zA-Zµ]*)$")

# Suffix -> millicores.
_CPU_UNITS = {"n": 1e-6, "u": 1e-3, "µ": 1e-3, "m": 1.0, "": 1000.0, "k": 1e6}
# Suffix -> bytes (binary Ki/Mi/… and decimal k/M/…).
_MEM_UNITS = {
    "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}


def _parse(quantity: str, units: dict[str, float]) -> float:
    m = _QUANTITY.match(quantity.strip())
    if not m or m.group(2) not in units:
        raise ValueError(f"unparseable quantity: {quantity!r}")
    return float(m.group(1)) * units[m.group(2)]


def parse_cpu_millicores(quantity: str) -> float:
    return _parse(quantity, _CPU_UNITS)


def parse_memory_bytes(quantity: str) -> float:
    return _parse(quantity, _MEM_UNITS)


# Knative injects this sidecar into every pod; exclude it so usage reflects the
# user's workload, not the platform proxy.
_SIDECAR = "queue-proxy"


def sum_usage(pod_metrics: list[dict]) -> ResourceUsage | None:
    """Sum cpu/memory over each pod's user container(s), ignoring the queue-proxy
    sidecar. None if there's nothing (e.g. the workload is scaled to zero)."""
    cpu_milli = 0.0
    mem_bytes = 0.0
    seen = False
    for pod in pod_metrics:
        for container in pod.get("containers", []) or []:
            if container.get("name") == _SIDECAR:
                continue
            usage = container.get("usage", {}) or {}
            if usage.get("cpu"):
                cpu_milli += parse_cpu_millicores(usage["cpu"])
                seen = True
            if usage.get("memory"):
                mem_bytes += parse_memory_bytes(usage["memory"])
                seen = True
    if not seen:
        return None
    return ResourceUsage(
        cpu=f"{round(cpu_milli)}m",
        memory=f"{round(mem_bytes / 2**20)}Mi",
    )
