"""Build controller: what a finished build does to the Knative Service it built.

The loop's whole job is one field, so most of these are about the ways it must
*decline* to write it - and about the read-back object surviving a round trip
that the API, which composes its KSVCs from scratch, never has to make.
"""

from __future__ import annotations

import pytest

from common import loop as common_loop
from common.cluster import NamespacedCluster, ResourceKind, clusters_for, select_local
from common.config import CommonSettings, RegionConfig
from common.errors import NotFoundError, ValidationError
from common.labels import (
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
    OFFERING_CONTAINER,
    OFFERING_FUNCTION,
)
from controller import main as controller_main
from controller.config import ControllerSettings
from controller.digest import deployed_image, needs_image, with_image
from controller.reconciler import IMAGE_SELECTOR, Reconciler

REPO = "registry.internal/acme/serverless/builders/payments/hello"
TAG = f"{REPO}:main"
DIGEST = f"{REPO}@sha256:{'a' * 64}"
NEWER = f"{REPO}@sha256:{'b' * 64}"
WORKLOAD = "hello-payments"


def _ksvc(image=TAG, offering=OFFERING_FUNCTION, name=WORKLOAD):
    """A Knative Service as the cluster hands it back - status and all."""
    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": "serverless-workloads",
            "labels": {LABEL_OFFERING: offering, LABEL_MANAGED_BY: MANAGED_BY_VALUE},
            "annotations": {
                "serverless.platform/host": "hello-payments.serverless.example.com",
                "kubectl.kubernetes.io/last-applied-configuration": '{"spec":{}}',
            },
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "generation": 4,
            "managedFields": [{"manager": "serverless-api"}],
            "resourceVersion": "12345",
            "selfLink": "/apis/serving.knative.dev/v1/services/hello-payments",
            "uid": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {"autoscaling.knative.dev/min-scale": "1"}},
                "spec": {
                    "containers": [
                        {
                            "image": image,
                            "env": [{"name": "LOG_LEVEL", "value": "info"}],
                            "resources": {"requests": {"cpu": "100m"}},
                        }
                    ],
                    "imagePullSecrets": [{"name": "serverless-registry-creds"}],
                },
            },
            "traffic": [{"latestRevision": True, "percent": 100}],
        },
        "status": {"latestReadyRevisionName": "hello-payments-00003"},
    }


def _image(latest=DIGEST, workload=WORKLOAD, ready="True", created="2026-08-05T12:00:00Z"):
    """A kpack Image as the cluster hands it back."""
    status = {"conditions": [{"type": "Ready", "status": ready}]}
    if latest:
        status["latestImage"] = latest
    metadata = {
        "name": workload if workload else "orphan-image",
        "labels": ({LABEL_WORKLOAD: workload} if workload else {}),
    }
    if created:
        metadata["creationTimestamp"] = created
    return {
        "apiVersion": "kpack.io/v1alpha2",
        "kind": "Image",
        "metadata": metadata,
        "status": status,
    }


class _FakeCluster:
    """One region: canned KSVCs and Images, recording every apply."""

    def __init__(
        self, region, ksvcs=None, images=None, version="7", fail_apply=False, fail_list=False
    ):
        self.region = region
        self.name = f"{region}-0"
        self._ksvcs = ksvcs if ksvcs is not None else {}
        self._images = images or []
        self._version = version
        self._fail_apply = fail_apply
        self._fail_list = fail_list
        self.applied = []
        self.deleted = []
        self.watch_calls = []
        self.list_calls = []
        self.closed = False
        self.events = []

    def get(self, kind, name=None, label_selector=None, namespace=None):
        assert kind is ResourceKind.KNATIVE_SERVICE
        if name not in self._ksvcs:
            raise NotFoundError(f"Service '{name}' not found")
        return self._ksvcs[name]

    def apply(self, manifest, namespace=None):
        if self._fail_apply:
            raise RuntimeError("apply refused")
        self.applied.append(manifest)
        self._ksvcs[manifest["metadata"]["name"]] = manifest
        return [manifest]

    def list_resources(self, kind, *, label_selector=None, namespace=None):
        if self._fail_list:
            raise RuntimeError("apiserver unreachable")
        self.list_calls.append((kind, label_selector))
        return list(self._images), self._version

    def delete(self, kind, name, namespace=None):
        self.deleted.append((kind, name))

    def watch(
        self,
        kind,
        *,
        resource_version=None,
        label_selector=None,
        timeout_seconds=None,
        namespace=None,
    ):
        self.watch_calls.append((kind, resource_version, label_selector, timeout_seconds))
        yield from (("MODIFIED", e) for e in self.events)

    def close(self):
        self.closed = True


def _reconciler(clusters, local="central"):
    """A Reconciler over fake regions (bypasses the real cluster construction).

    Peers are still passed in even though the loop only wires the local one, so
    a test can assert it never touches them.
    """
    reconciler = object.__new__(Reconciler)
    reconciler._local = clusters[local]
    # The real constructor binds the workloads namespace over the local
    # cluster; the fakes are duck-typed under the same view.
    reconciler._bound = NamespacedCluster(clusters[local], "serverless-workloads")
    reconciler._gc = None
    return reconciler


# --------------------------------------------------------------------------- #
# digest: reading and rewriting the workload                                    #
# --------------------------------------------------------------------------- #


def test_deployed_image_reads_the_first_container():
    assert deployed_image(_ksvc()) == TAG


@pytest.mark.parametrize(
    "ksvc",
    [
        {},
        {"spec": {"template": {"spec": {"containers": []}}}},
        {"spec": {"template": {"spec": {"containers": [{}]}}}},
    ],
)
def test_deployed_image_is_none_when_there_is_nothing_to_read(ksvc):
    assert deployed_image(ksvc) is None


def test_a_new_digest_on_a_function_is_wanted():
    assert needs_image(_ksvc(), DIGEST) is True


def test_the_digest_it_already_runs_is_not_wanted():
    # The loop's normal outcome, and why a resync every few minutes is free.
    assert needs_image(_ksvc(image=DIGEST), DIGEST) is False


def test_a_container_offering_is_never_written():
    # A container that reused a deleted function's name must not inherit its image.
    assert needs_image(_ksvc(offering=OFFERING_CONTAINER), DIGEST) is False


def test_a_digest_from_a_moved_repository_is_wanted():
    # The layout is configuration, and after the create nothing else writes the
    # image - refusing this would strand the workload on the old repository.
    moved = "registry.internal/acme/payments/hello@sha256:" + "c" * 64
    assert needs_image(_ksvc(), moved) is True


def test_a_workload_with_no_image_is_left_alone():
    assert needs_image(_ksvc(image=None), DIGEST) is False


def test_with_image_strips_everything_the_server_owns():
    out = with_image(_ksvc(), DIGEST)

    assert "status" not in out
    for key in ("creationTimestamp", "generation", "managedFields", "resourceVersion"):
        assert key not in out["metadata"], key
    assert "selfLink" not in out["metadata"]
    assert "uid" not in out["metadata"]
    assert "kubectl.kubernetes.io/last-applied-configuration" not in out["metadata"]["annotations"]


def test_with_image_drops_a_pinned_revision_name():
    # Knative rejects a template whose name is unchanged while its content is not,
    # so a hand-pinned name would wedge every roll-out of this function.
    ksvc = _ksvc()
    ksvc["spec"]["template"]["metadata"]["name"] = "hello-payments-00003"

    assert "name" not in with_image(ksvc, DIGEST)["spec"]["template"]["metadata"]


def test_with_image_changes_the_image_and_nothing_else():
    ksvc = _ksvc()
    out = with_image(ksvc, DIGEST)

    container = out["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == DIGEST
    assert container["env"] == [{"name": "LOG_LEVEL", "value": "info"}]
    assert container["resources"] == {"requests": {"cpu": "100m"}}
    assert out["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "serverless-registry-creds"}
    ]
    assert out["spec"]["template"]["metadata"]["annotations"] == {
        "autoscaling.knative.dev/min-scale": "1"
    }
    assert out["spec"]["traffic"] == [{"latestRevision": True, "percent": 100}]
    assert out["metadata"]["labels"] == ksvc["metadata"]["labels"]
    # The input is the live object the caller may still be reading.
    assert ksvc["spec"]["template"]["spec"]["containers"][0]["image"] == TAG
    assert "status" in ksvc


# --------------------------------------------------------------------------- #
# reconcile: one Image, its own region                                            #
# --------------------------------------------------------------------------- #


def test_a_finished_build_is_rolled_out_to_the_local_ksvc_only():
    """A region builds what it runs, so a peer's digest is not this loop's to write.

    The peer has its own Image, its own build and its own controller; writing
    to it would push a reference into a registry it does not pull from.
    """
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})
    south = _FakeCluster("south", {WORKLOAD: _ksvc()})

    _reconciler({"central": central, "south": south}).reconcile(_image())

    assert deployed_image(central.applied[0]) == DIGEST
    assert south.applied == []


def test_an_image_whose_workload_is_gone_writes_nothing():
    """A delete that has not finished cascading, or a pre-migration leftover."""
    central = _FakeCluster("central", {})

    assert _reconciler({"central": central}).reconcile(_image()) is False
    assert central.applied == []


def test_an_apply_failure_is_swallowed_so_the_loop_survives():
    """The next resync retries; raising here would take the watch down with it."""
    broken = _FakeCluster("central", {WORKLOAD: _ksvc()}, fail_apply=True)

    assert _reconciler({"central": broken}).reconcile(_image()) is False
    assert broken.applied == []


def test_an_image_that_has_never_built_writes_nothing():
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})

    _reconciler({"central": central}).reconcile(_image(latest=None))

    assert central.applied == []


def test_an_image_with_no_workload_label_writes_nothing():
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})

    _reconciler({"central": central}).reconcile(_image(workload=None))

    assert central.applied == []


def test_reconciling_the_digest_already_deployed_is_a_no_op():
    central = _FakeCluster("central", {WORKLOAD: _ksvc(image=DIGEST)})

    _reconciler({"central": central}).reconcile(_image())

    assert central.applied == []


def test_a_failing_build_still_propagates_the_last_successful_digest():
    # latestImage is the last SUCCESSFUL build, so a function whose newest build
    # failed keeps rolling out the one before it rather than stalling.
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})

    _reconciler({"central": central}).reconcile(_image(ready="False"))

    assert deployed_image(central.applied[0]) == DIGEST


def test_an_unreadable_workload_is_logged_and_skipped():
    """Not a 404 but a broken read: it must not take the watch down."""

    class _Unreadable(_FakeCluster):
        def get(self, kind, name=None, label_selector=None, namespace=None):
            raise RuntimeError("apiserver said no")

    unreadable = _Unreadable("central", {WORKLOAD: _ksvc()})

    assert _reconciler({"central": unreadable}).reconcile(_image()) is False
    assert unreadable.applied == []


# --------------------------------------------------------------------------- #
# the loop: resync, then follow                                                 #
# --------------------------------------------------------------------------- #


def test_resync_reconciles_every_local_image_and_returns_the_watch_position():
    other = "second-payments"
    central = _FakeCluster(
        "central",
        {WORKLOAD: _ksvc(), other: _ksvc(name=other)},
        images=[_image(), _image(latest=NEWER, workload=other)],
        version="4242",
    )

    version = _reconciler({"central": central}).resync()

    assert version == "4242"
    assert central.list_calls == [(ResourceKind.KPACK_IMAGE, IMAGE_SELECTOR)]
    assert {deployed_image(m) for m in central.applied} == {DIGEST, NEWER}


def test_the_selector_takes_only_this_platforms_function_builds():
    # A kpack install is shared infrastructure; another team's Image is not ours.
    assert IMAGE_SELECTOR == (
        f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE},{LABEL_OFFERING}={OFFERING_FUNCTION}"
    )


def test_follow_resumes_the_watch_from_the_resync_and_reconciles_each_event():
    central = _FakeCluster("central", {WORKLOAD: _ksvc()}, images=[], version="99")
    central.events = [_image(latest=NEWER)]

    _reconciler({"central": central}).follow(120)

    assert central.watch_calls == [(ResourceKind.KPACK_IMAGE, "99", IMAGE_SELECTOR, 120)]
    assert deployed_image(central.applied[0]) == NEWER


def test_only_the_local_region_is_watched():
    central = _FakeCluster("central", {}, images=[])
    south = _FakeCluster("south", {}, images=[])

    _reconciler({"central": central, "south": south}, local="south").follow(60)

    assert central.watch_calls == []
    assert len(south.watch_calls) == 1


def test_close_releases_the_local_client():
    central, south = _FakeCluster("central"), _FakeCluster("south")

    _reconciler({"central": central, "south": south}).close()

    assert central.closed
    assert not south.closed  # never opened - the loop holds one region


# --------------------------------------------------------------------------- #
# wiring: region selection, settings, signals                                     #
# --------------------------------------------------------------------------- #


def _settings(local):
    return CommonSettings(
        regions=[
            RegionConfig(name="central", cluster="central-0"),
            RegionConfig(name="south", cluster="south-0"),
        ],
        local_region=local,
    )


@pytest.mark.parametrize(
    "local, expected", [("south", "south"), ("south-0", "south"), (None, "central")]
)
def test_the_local_region_resolves_by_region_name_cluster_name_or_first(local, expected):
    clusters = clusters_for(_settings(local))

    assert select_local(clusters, local).region == expected


def test_a_controller_with_no_regions_refuses_to_start():
    # It would have nothing to watch and nowhere to write.
    with pytest.raises(ValidationError):
        Reconciler(CommonSettings(regions=[]))


def test_the_reconciler_watches_the_configured_local_region():
    reconciler = Reconciler(_settings("south"))
    try:
        assert reconciler.local.region == "south"
    finally:
        reconciler.close()


def test_resync_seconds_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERVERLESS_RESYNC_SECONDS", "45")

    assert ControllerSettings().resync_seconds == 45


def test_a_zero_resync_interval_is_rejected():
    # It would busy-loop the apiserver with relists.
    with pytest.raises(ValueError):
        ControllerSettings(resync_seconds=0)


def test_the_loop_backs_off_after_a_failed_pass_and_keeps_going(monkeypatch):
    slept = []
    monkeypatch.setattr(common_loop.time, "sleep", slept.append)
    passes = iter([RuntimeError("apiserver down"), None, SystemExit(0)])

    class _Flaky:
        def follow(self, timeout):
            outcome = next(passes)
            if outcome is not None:
                raise outcome

    with pytest.raises(SystemExit):
        controller_main.loop(_Flaky(), ControllerSettings(error_backoff_seconds=2.5))

    # The failed pass sleeps the error backoff. The clean pass that follows ended
    # instantly, so it is paced to the minimum pass duration - the floor that
    # keeps a stream closed at the door from becoming back-to-back relists.
    assert slept[0] == 2.5
    assert len(slept) == 2
    assert 0 < slept[1] <= common_loop.MIN_PASS_SECONDS


def test_run_installs_the_signal_handlers_and_releases_the_clusters(monkeypatch):
    # The entrypoint: a typo here is a pod that never starts, which no other test
    # would catch.
    signals, closed = {}, []

    class _Reconciler:
        local = type("Region", (), {"region": "central"})

        def close(self):
            closed.append(True)

    monkeypatch.setattr(common_loop.signal, "signal", lambda s, h: signals.setdefault(s, h))
    monkeypatch.setattr(controller_main, "Reconciler", lambda settings, **kw: _Reconciler())
    monkeypatch.setattr(controller_main, "loop", lambda r, s: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        controller_main.run()

    assert set(signals) == {common_loop.signal.SIGTERM, common_loop.signal.SIGINT}
    assert closed == [True]
