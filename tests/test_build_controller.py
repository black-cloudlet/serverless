"""Build controller: what a finished build does to the Knative Service it built.

The loop's whole job is one field, so most of these are about the ways it must
*decline* to write it - and about the read-back object surviving a round trip
that the API, which composes its KSVCs from scratch, never has to make.
"""

from __future__ import annotations

import pytest

from common.cluster import ResourceKind, clusters_for, select_local
from common.config import CommonSettings, SiteConfig
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
        "name": f"fn-{workload}" if workload else "fn-orphan",
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
    """One site: canned KSVCs and Images, recording every apply."""

    def __init__(
        self, site, ksvcs=None, images=None, version="7", fail_apply=False, fail_list=False
    ):
        self.site = site
        self.name = f"{site}-0"
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

    def get(self, kind, name=None, label_selector=None):
        assert kind is ResourceKind.KNATIVE_SERVICE
        if name not in self._ksvcs:
            raise NotFoundError(f"Service '{name}' not found")
        return self._ksvcs[name]

    def apply(self, manifest):
        if self._fail_apply:
            raise RuntimeError("apply refused")
        self.applied.append(manifest)
        self._ksvcs[manifest["metadata"]["name"]] = manifest
        return [manifest]

    def list_resources(self, kind, *, label_selector=None):
        if self._fail_list:
            raise RuntimeError("apiserver unreachable")
        self.list_calls.append((kind, label_selector))
        return list(self._images), self._version

    def delete(self, kind, name):
        self.deleted.append((kind, name))

    def watch(self, kind, *, resource_version=None, label_selector=None, timeout_seconds=None):
        self.watch_calls.append((kind, resource_version, label_selector, timeout_seconds))
        yield from (("MODIFIED", e) for e in self.events)

    def close(self):
        self.closed = True


def _reconciler(clusters, local="central", prune=False):
    """A Reconciler over fake sites (bypasses the real cluster construction)."""
    reconciler = object.__new__(Reconciler)
    reconciler._clusters = clusters
    reconciler._local = clusters[local]
    reconciler._prune_orphans = prune
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
# reconcile: one Image, every site                                              #
# --------------------------------------------------------------------------- #


def test_a_finished_build_is_rolled_out_to_every_site():
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})
    south = _FakeCluster("south", {WORKLOAD: _ksvc()})

    _reconciler({"central": central, "south": south}).reconcile(_image())

    for cluster in (central, south):
        assert deployed_image(cluster.applied[0]) == DIGEST


def test_a_site_that_does_not_run_the_function_is_skipped_without_failing_the_rest():
    central = _FakeCluster("central", {WORKLOAD: _ksvc()})
    south = _FakeCluster("south", {})  # deployed to central only

    _reconciler({"central": central, "south": south}).reconcile(_image())

    assert deployed_image(central.applied[0]) == DIGEST
    assert south.applied == []


def test_one_sites_apply_failure_does_not_stop_the_other():
    broken = _FakeCluster("central", {WORKLOAD: _ksvc()}, fail_apply=True)
    south = _FakeCluster("south", {WORKLOAD: _ksvc()})

    _reconciler({"central": broken, "south": south}).reconcile(_image())

    assert broken.applied == []
    assert deployed_image(south.applied[0]) == DIGEST


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


def test_an_unreadable_site_is_logged_and_skipped():
    class _Unreadable(_FakeCluster):
        def get(self, kind, name=None, label_selector=None):
            raise RuntimeError("apiserver said no")

    unreadable = _Unreadable("central", {WORKLOAD: _ksvc()})
    south = _FakeCluster("south", {WORKLOAD: _ksvc()})

    _reconciler({"central": unreadable, "south": south}).reconcile(_image())

    assert unreadable.applied == []
    assert deployed_image(south.applied[0]) == DIGEST


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


def test_only_the_local_site_is_watched():
    central = _FakeCluster("central", {}, images=[])
    south = _FakeCluster("south", {}, images=[])

    _reconciler({"central": central, "south": south}, local="south").follow(60)

    assert central.watch_calls == []
    assert len(south.watch_calls) == 1


def test_close_releases_every_site():
    central, south = _FakeCluster("central"), _FakeCluster("south")

    _reconciler({"central": central, "south": south}).close()

    assert central.closed and south.closed


# --------------------------------------------------------------------------- #
# wiring: site selection, settings, signals                                     #
# --------------------------------------------------------------------------- #


def _settings(local):
    return CommonSettings(
        sites=[
            SiteConfig(name="central", cluster="central-0"),
            SiteConfig(name="south", cluster="south-0"),
        ],
        local_site=local,
    )


@pytest.mark.parametrize(
    "local, expected", [("south", "south"), ("south-0", "south"), (None, "central")]
)
def test_the_local_site_resolves_by_site_name_cluster_name_or_first(local, expected):
    clusters = clusters_for(_settings(local))

    assert select_local(clusters, local).site == expected


def test_a_controller_with_no_sites_refuses_to_start():
    # It would have nothing to watch and nowhere to write.
    with pytest.raises(ValidationError):
        Reconciler(CommonSettings(sites=[]))


def test_the_reconciler_watches_the_configured_local_site():
    reconciler = Reconciler(_settings("south"))
    try:
        assert reconciler.local.site == "south"
        assert set(reconciler._clusters) == {"central", "south"}
    finally:
        reconciler.close()


def test_resync_seconds_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERVERLESS_RESYNC_SECONDS", "45")

    assert ControllerSettings().resync_seconds == 45


def test_a_zero_resync_interval_is_rejected():
    # It would busy-loop the apiserver with relists.
    with pytest.raises(ValueError):
        ControllerSettings(resync_seconds=0)


def test_terminating_raises_so_a_blocking_watch_unwinds():
    with pytest.raises(SystemExit):
        controller_main._terminate(15, None)


def test_the_loop_backs_off_after_a_failed_pass_and_keeps_going(monkeypatch):
    slept = []
    monkeypatch.setattr(controller_main.time, "sleep", slept.append)
    passes = iter([RuntimeError("apiserver down"), None, SystemExit(0)])

    class _Flaky:
        def follow(self, timeout):
            outcome = next(passes)
            if outcome is not None:
                raise outcome

    with pytest.raises(SystemExit):
        controller_main.loop(_Flaky(), ControllerSettings(error_backoff_seconds=2.5))

    # One sleep, for the one failure - a watch that merely ended is not one.
    assert slept == [2.5]


def test_run_installs_the_signal_handlers_and_releases_the_clusters(monkeypatch):
    # The entrypoint: a typo here is a pod that never starts, which no other test
    # would catch.
    signals, closed = {}, []

    class _Reconciler:
        local = type("Site", (), {"site": "central"})

        def close(self):
            closed.append(True)

    monkeypatch.setattr(controller_main.signal, "signal", lambda s, h: signals.setdefault(s, h))
    monkeypatch.setattr(controller_main, "Reconciler", lambda settings, **kw: _Reconciler())
    monkeypatch.setattr(controller_main, "loop", lambda r, s: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        controller_main.run()

    assert set(signals) == {controller_main.signal.SIGTERM, controller_main.signal.SIGINT}
    assert closed == [True]


# --------------------------------------------------------------------------- #
# prune: the Images a switchover strands                                        #
# --------------------------------------------------------------------------- #

OLD, NEW = "2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z"


def _sites(local_created, remote_created, prune=True):
    """Two sites holding an Image for the same function, built at different times."""
    central = _FakeCluster("central", {WORKLOAD: _ksvc()}, images=[_image(created=local_created)])
    south = _FakeCluster("south", {WORKLOAD: _ksvc()}, images=[_image(created=remote_created)])
    return central, south, _reconciler({"central": central, "south": south}, prune=prune)


async def test_the_newer_image_prunes_the_one_a_switchover_stranded():
    central, south, reconciler = _sites(local_created=NEW, remote_created=OLD)

    reconciler.resync()

    assert south.deleted == [(ResourceKind.KPACK_IMAGE, f"fn-{WORKLOAD}")]
    assert central.deleted == []


async def test_the_older_site_prunes_nothing():
    # The other half of the pair above: exactly one site acts, so the two can
    # never delete each other's Images.
    central, south, reconciler = _sites(local_created=OLD, remote_created=NEW)

    reconciler.resync()

    assert (central.deleted, south.deleted) == ([], [])


async def test_two_images_of_the_same_age_are_both_left_alone():
    # Clock skew between two API servers must not read as "superseded".
    central, south, reconciler = _sites(local_created=NEW, remote_created=NEW)

    reconciler.resync()

    assert south.deleted == []


async def test_an_image_with_no_timestamp_is_never_pruned():
    central = _FakeCluster("central", images=[_image(created=NEW)])
    south = _FakeCluster("south", images=[_image(created=None)])

    _reconciler({"central": central, "south": south}, prune=True).resync()

    assert south.deleted == []


async def test_a_function_only_this_site_builds_is_not_touched():
    # The normal case: one Image, one site, nothing to compare against.
    central = _FakeCluster("central", images=[_image(created=NEW)])
    south = _FakeCluster("south", images=[])

    _reconciler({"central": central, "south": south}, prune=True).resync()

    assert south.deleted == []


async def test_a_site_holding_no_images_prunes_nothing():
    """The dangerous case: an empty local site must not read as "I superseded it".

    True of a controller starting on a site that has never built, and of one
    whose own Images were pruned by the other side a moment ago.
    """
    empty = _FakeCluster("central", images=[])
    holder = _FakeCluster("south", images=[_image(created=OLD)])

    _reconciler({"central": empty, "south": holder}, prune=True).resync()

    assert holder.deleted == []


async def test_a_site_that_cannot_be_listed_stops_the_whole_prune():
    # Deciding what is stranded from a partial view is how everything gets deleted.
    central = _FakeCluster("central", images=[_image(created=NEW)])
    unreadable = _FakeCluster("south", images=[_image(created=OLD)], fail_list=True)
    third = _FakeCluster("west", images=[_image(created=OLD)])

    _reconciler({"central": central, "south": unreadable, "west": third}, prune=True).resync()

    assert third.deleted == []


async def test_pruning_off_leaves_the_stranded_image_alone():
    central, south, reconciler = _sites(local_created=NEW, remote_created=OLD, prune=False)

    reconciler.resync()

    assert south.deleted == []


async def test_a_prune_failure_does_not_stop_the_rest():
    class _Undeletable(_FakeCluster):
        def delete(self, kind, name):
            raise RuntimeError("delete refused")

    central = _FakeCluster("central", images=[_image(created=NEW)])
    broken = _Undeletable("south", images=[_image(created=OLD)])
    west = _FakeCluster("west", images=[_image(created=OLD)])

    pruned = _reconciler({"central": central, "south": broken, "west": west}, prune=True).prune(
        [_image(created=NEW)]
    )

    assert pruned == 1 and west.deleted
