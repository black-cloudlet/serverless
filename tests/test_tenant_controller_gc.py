"""Namespace GC: the grace period, the keep annotation, and what is never touched.

The one destructive act in the tenant controller, so the tests lean negative:
a namespace is deleted only when every condition holds at once - managed by
label, continuously empty past the grace, not kept, deletion enabled - and
everything short of that is a stamp, a cleared stamp, or a loud log line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.cluster import ResourceKind
from common.labels import (
    ANNOTATION_EMPTY_SINCE,
    ANNOTATION_KEEP,
    LABEL_MANAGED_BY,
    TENANT_CONTROLLER_VALUE,
)
from tenant_controller import gc as gc_mod
from tenant_controller.config import TenantControllerSettings
from tenant_controller.gc import NamespaceGC

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


def _namespace(name, *, annotations=None, managed=True, deleting=False):
    meta = {
        "name": name,
        "labels": {LABEL_MANAGED_BY: TENANT_CONTROLLER_VALUE} if managed else {},
        "annotations": annotations or {},
    }
    if deleting:
        meta["deletionTimestamp"] = "2026-08-30T11:00:00+00:00"
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": meta}


class _Cluster:
    """The local cluster: managed namespaces, per-namespace workloads, a log of writes."""

    region = "central"

    def __init__(self, namespaces=(), workloads=None):
        self._namespaces = list(namespaces)
        self._workloads = workloads or {}  # namespace -> list of KSVCs
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def get(self, kind, name=None, label_selector=None, *, namespace, field_selector=None):
        if kind == ResourceKind.NAMESPACE:
            assert label_selector == f"{LABEL_MANAGED_BY}={TENANT_CONTROLLER_VALUE}"
            return list(self._namespaces)
        if kind == ResourceKind.KNATIVE_SERVICE:
            # The sweep reads one cluster-wide listing, never per namespace.
            assert namespace is None
            return [
                {**w, "metadata": {**(w.get("metadata") or {}), "namespace": ns}}
                for ns, items in self._workloads.items()
                for w in items
            ]
        raise AssertionError(f"unexpected read of {kind}")

    def patch(self, kind, name, body, *, namespace):
        assert kind == ResourceKind.NAMESPACE and namespace is None
        self.patched.append((name, body))

    def delete(self, kind, name, *, namespace):
        assert kind == ResourceKind.NAMESPACE and namespace is None
        self.deleted.append(name)


def _gc(cluster, *, enabled=True, grace=3600, monkeypatch=None, now=NOW):
    settings = TenantControllerSettings(
        regions=[], gc_enabled=enabled, gc_grace_seconds=grace, gc_interval_seconds=60
    )
    if monkeypatch is not None:
        monkeypatch.setattr(gc_mod, "_now", lambda: now)
    return NamespaceGC(settings, cluster)


def _stamped(seconds_ago):
    return {ANNOTATION_EMPTY_SINCE: (NOW - timedelta(seconds=seconds_ago)).isoformat()}


def _annotations_patched(cluster, name):
    return [b["metadata"]["annotations"] for n, b in cluster.patched if n == name]


# --- the grace period -------------------------------------------------------


def test_a_namespace_first_seen_empty_is_stamped_not_deleted(monkeypatch):
    cluster = _Cluster([_namespace("team-serverless")])
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert _annotations_patched(cluster, "team-serverless") == [
        {ANNOTATION_EMPTY_SINCE: NOW.isoformat()}
    ]


def test_empty_within_the_grace_is_left_alone(monkeypatch):
    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(100))])
    _gc(cluster, grace=3600, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert cluster.patched == []


def test_empty_past_the_grace_is_deleted(monkeypatch):
    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(4000))])
    _gc(cluster, grace=3600, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == ["team-serverless"]


def test_a_workload_appearing_clears_the_stamp(monkeypatch):
    """The grace is CONTINUOUS emptiness: a workload that came and went again
    restarts the clock, it does not resume it."""
    cluster = _Cluster(
        [_namespace("team-serverless", annotations=_stamped(4000))],
        workloads={"team-serverless": [{"kind": "Service"}]},
    )
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert _annotations_patched(cluster, "team-serverless") == [{ANNOTATION_EMPTY_SINCE: None}]


def test_an_unreadable_stamp_restarts_the_clock_rather_than_counting(monkeypatch):
    """A stamp that cannot be parsed must not read as 'ancient': re-stamp now."""
    cluster = _Cluster(
        [_namespace("team-serverless", annotations={ANNOTATION_EMPTY_SINCE: "not-a-time"})]
    )
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert _annotations_patched(cluster, "team-serverless") == [
        {ANNOTATION_EMPTY_SINCE: NOW.isoformat()}
    ]


# --- the refusals -----------------------------------------------------------


def test_the_keep_annotation_wins_over_everything(monkeypatch):
    cluster = _Cluster(
        [_namespace("team-serverless", annotations={**_stamped(999999), ANNOTATION_KEEP: "audit"})]
    )
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []


def test_disabled_gc_stamps_but_never_deletes(monkeypatch, caplog):
    """Stamping runs regardless, so enabling GC later does not restart every
    clock - and the refusal to delete is a log line, not silence."""
    cluster = _Cluster(
        [
            _namespace("fresh-serverless"),
            _namespace("old-serverless", annotations=_stamped(999999)),
        ]
    )
    with caplog.at_level("INFO"):
        _gc(cluster, enabled=False, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert _annotations_patched(cluster, "fresh-serverless") == [
        {ANNOTATION_EMPTY_SINCE: NOW.isoformat()}
    ]
    assert any("deletion is disabled" in r.message for r in caplog.records)


def test_a_namespace_already_terminating_is_skipped(monkeypatch):
    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(999999), deleting=True)])
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []
    assert cluster.patched == []


def test_only_the_managed_label_selector_is_ever_listed():
    """The GC's whole view of the world is the label-selected listing; the
    fake asserts the selector, so an unlabeled namespace is unreachable by
    construction."""
    cluster = _Cluster([])
    _gc(cluster).sweep()
    assert cluster.deleted == [] and cluster.patched == []


# --- sweep robustness -------------------------------------------------------


def test_one_failing_namespace_does_not_end_the_sweep(monkeypatch):
    """The listing order is stable, so an aborting error would starve every
    namespace after the failing one, deterministically."""
    boom = _namespace("boom-serverless")  # unstamped: the sweep will try to stamp it
    fine = _namespace("fine-serverless", annotations=_stamped(4000))
    cluster = _Cluster([boom, fine])
    real_patch = cluster.patch

    def patch(kind, name, body, *, namespace):
        if name == "boom-serverless":
            raise RuntimeError("apiserver hiccup")
        return real_patch(kind, name, body, namespace=namespace)

    cluster.patch = patch
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == ["fine-serverless"]


def test_a_read_failure_never_deletes(monkeypatch):
    """An unreadable workload listing is not an empty one - the fail-closed
    rule, on the destructive side: the whole sweep aborts (and is contained
    by the thread body), deleting nothing."""
    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(4000))])

    def get(kind, name=None, label_selector=None, *, namespace, field_selector=None):
        if kind == ResourceKind.NAMESPACE:
            return list(cluster._namespaces)
        raise RuntimeError("apiserver unreachable")

    cluster.get = get
    with pytest.raises(RuntimeError, match="apiserver unreachable"):
        _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == []


def test_maybe_sweep_paces_itself_and_runs_off_the_loop(monkeypatch):
    cluster = _Cluster([_namespace("team-serverless")])
    gc = _gc(cluster, monkeypatch=monkeypatch)

    gc.maybe_sweep()
    gc.wait(5)
    gc.maybe_sweep()  # within the interval: no second sweep
    gc.wait(5)

    assert _annotations_patched(cluster, "team-serverless") == [
        {ANNOTATION_EMPTY_SINCE: NOW.isoformat()}
    ]


def test_a_sweep_failure_stays_inside_the_gc(monkeypatch):
    cluster = _Cluster([])

    def bad_get(*args, **kwargs):
        raise RuntimeError("listing failed")

    cluster.get = bad_get
    gc = _gc(cluster, monkeypatch=monkeypatch)
    gc.maybe_sweep()
    gc.wait(5)  # the loop never sees the raise; nothing to assert beyond no propagation


def test_a_regions_emptiness_is_judged_locally(monkeypatch):
    """A workload with a subset regions list leaves the peer's namespace
    legitimately empty: this cluster collects its own copy and never asks the
    peer. (The GC holds one cluster by construction; the fake asserting every
    read is what proves no cross-region call exists to make.)"""
    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(4000))])
    _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert cluster.deleted == ["team-serverless"]


def test_deletion_between_list_and_delete_is_not_a_failure(monkeypatch, caplog):
    from common.errors import NotFoundError

    cluster = _Cluster([_namespace("team-serverless", annotations=_stamped(4000))])

    def delete(kind, name, *, namespace):
        raise NotFoundError(f"{name} gone")

    cluster.delete = delete
    with caplog.at_level("INFO"):
        _gc(cluster, monkeypatch=monkeypatch).sweep()

    assert not any("failed" in r.message for r in caplog.records if "sweeping" in r.message)


@pytest.mark.parametrize(
    "enabled, expected", [(True, "namespace GC on"), (False, "namespace GC off")]
)
def test_the_gc_states_its_posture_at_startup(caplog, enabled, expected):
    with caplog.at_level("INFO"):
        _gc(_Cluster([]), enabled=enabled)
    assert any(expected in r.message for r in caplog.records)
