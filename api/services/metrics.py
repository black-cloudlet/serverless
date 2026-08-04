"""Parse and aggregate live resource usage from the metrics API (PodMetrics).

``metrics.k8s.io`` reports per-container usage as Kubernetes *quantity* strings
(e.g. cpu ``"1234567n"``, memory ``"123456Ki"``). We sum across all containers
of all the workload's pods into one cpu/memory figure per site
(:func:`sum_usage`), and project the same parse per pod (:func:`pod_usage`) for
the status view's breakdown.
"""

from __future__ import annotations

import re

from api.models.common import PodUsage, ResourceUsage

_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)([a-zA-Zµ]*)$")

# Suffix -> millicores.
_CPU_UNITS = {"n": 1e-6, "u": 1e-3, "µ": 1e-3, "m": 1.0, "": 1000.0, "k": 1e6}
# Suffix -> bytes (binary Ki/Mi/… and decimal k/M/…).
_MEM_UNITS = {
    "": 1.0,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def _parse(quantity: str, units: dict[str, float]) -> float:
    """Parse a ``<number><unit>`` quantity into a float using ``units``.

    Raises:
        ValueError: If the quantity is malformed or the unit is unknown.
    """
    m = _QUANTITY.match(quantity.strip())
    if not m or m.group(2) not in units:
        raise ValueError(f"unparseable quantity: {quantity!r}")
    return float(m.group(1)) * units[m.group(2)]


def parse_cpu_millicores(quantity: str) -> float:
    """Parse a Kubernetes CPU quantity (e.g. ``250m``, ``1``) into millicores.

    Args:
        quantity: The CPU quantity string.

    Returns:
        The value in millicores.
    """
    return _parse(quantity, _CPU_UNITS)


def parse_memory_bytes(quantity: str) -> float:
    """Parse a Kubernetes memory quantity (e.g. ``512Mi``, ``1Gi``) into bytes.

    Args:
        quantity: The memory quantity string.

    Returns:
        The value in bytes.
    """
    return _parse(quantity, _MEM_UNITS)


# Knative injects this sidecar into every pod; exclude it so usage reflects the
# user's workload, not the platform proxy.
_SIDECAR = "queue-proxy"
# Copied onto the PodMetrics from the pod it measures, so the breakdown can say
# which revision a replica belongs to without a second read.
_REVISION_LABEL = "serving.knative.dev/revision"


def _quantities(cpu_milli: float, mem_bytes: float) -> ResourceUsage:
    """Project raw totals into the API's quantity strings (one rounding rule)."""
    return ResourceUsage(cpu=f"{round(cpu_milli)}m", memory=f"{round(mem_bytes / 2**20)}Mi")


def _pod_totals(pod_metrics: list[dict]) -> list[tuple[str, str | None, float, float]]:
    """Per-pod ``(name, revision, cpu millicores, memory bytes)``, unrounded.

    The single parse behind both :func:`sum_usage` and :func:`pod_usage`, so a
    site's total and the breakdown it is the sum of cannot disagree. Rounding is
    deliberately left to the caller: summing already-rounded per-pod figures
    would drift from the total by up to half a unit per replica.

    A pod whose containers report nothing is omitted rather than carried as a
    zero - "not measured yet" and "idle" are different states, and a fresh pod
    the metrics-server has not scraped yet is the first one.

    Args:
        pod_metrics: PodMetrics items for the workload's pods.

    Returns:
        One tuple per pod that reported usage.
    """
    totals: list[tuple[str, str | None, float, float]] = []
    for pod in pod_metrics:
        meta = pod.get("metadata", {}) or {}
        cpu_milli = 0.0
        mem_bytes = 0.0
        seen = False
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
        if seen:
            revision = (meta.get("labels", {}) or {}).get(_REVISION_LABEL)
            totals.append((meta.get("name", ""), revision, cpu_milli, mem_bytes))
    return totals


def sum_usage(pod_metrics: list[dict]) -> ResourceUsage | None:
    """Sum cpu/memory over each pod's user container(s), ignoring queue-proxy.

    Args:
        pod_metrics: PodMetrics items for the workload's pods.

    Returns:
        The summed usage, or None if there's nothing (e.g. the workload is
        scaled to zero).
    """
    totals = _pod_totals(pod_metrics)
    if not totals:
        return None
    return _quantities(sum(t[2] for t in totals), sum(t[3] for t in totals))


def pod_usage(pod_metrics: list[dict]) -> list[PodUsage]:
    """The same usage broken down per replica, for the status view.

    Costs no extra cluster call - the PodMetrics list already carries per-pod
    data, which :func:`sum_usage` collapses. Reported in the order the metrics
    API returned, so a client can sort on whichever field it renders by.

    Args:
        pod_metrics: PodMetrics items for the workload's pods.

    Returns:
        One entry per pod that reported usage; empty when none did.
    """
    return [
        PodUsage(pod=name, revision=revision, usage=_quantities(cpu_milli, mem_bytes))
        for name, revision, cpu_milli, mem_bytes in _pod_totals(pod_metrics)
    ]
