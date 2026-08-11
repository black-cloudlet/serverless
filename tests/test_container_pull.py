"""POST /containers/{name}/pull - re-resolve a mutable tag.

Knative pins a revision to the digest it resolved when the revision was created,
so a tag pushed over is never picked up. These cover the stamp that makes it cut
a new one, and the carry-forward that stops an ordinary update cutting another.
"""

from __future__ import annotations

import pytest
from cloudlet_apis.auth import Principal
from fastapi import BackgroundTasks

from api.core.config import Settings
from api.models.common import ANNOTATION_PULL_STAMP, LABEL_GROUP, LABEL_OFFERING, Scaling
from api.services.container import ContainerService
from api.services.manifests.ksvc import build_ksvc
from api.services.offering import CONTAINER
from api.services.regions.deployer import Deployer
from api.services.regions.region_read import existing_state
from api.services.workloads import WorkloadService
from common.errors import ForbiddenError, NotFoundError, ValidationError
from common.labels import OFFERING_CONTAINER

USER = Principal(subject="u", username="alice", groups=["team"])
STAMP = "2026-08-05T21:00:00+00:00"


def _ksvc(image="reg/x:1", offering=OFFERING_CONTAINER, stamp=None):
    annotations = {"serverless.platform/host": "app-team.example.com"}
    if stamp:
        annotations[ANNOTATION_PULL_STAMP] = stamp
    return {
        "metadata": {
            "name": "app-team",
            "labels": {LABEL_GROUP: "team", LABEL_OFFERING: offering},
            "annotations": annotations,
        },
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
    }


class _FakeCluster:
    """One region: a canned KSVC, recording every patch."""

    def __init__(self, region, ksvc=None):
        self.region = self.name = region
        self._ksvc = ksvc
        self.patches = []

    def get(self, kind, name=None, label_selector=None):
        if self._ksvc is None:
            raise NotFoundError(f"{name} not found")
        return self._ksvc

    def patch(self, kind, name, body):
        if self._ksvc is None:
            raise NotFoundError(f"{name} not found")
        self.patches.append((kind, name, body))
        return self._ksvc


class _NullBuilder:
    pull_secret = "reg-creds"

    def image_ref(self, req, region=None):
        return "reg/built:1"

    def status(self, cluster, name, group):
        return None

    def statuses(self, cluster, group):
        return {}


def _service(clusters):
    settings = Settings(
        regions=[{"name": s, "cluster": f"{s}-0"} for s in clusters], auth_enabled=False
    )
    deployer = Deployer(settings)
    deployer._clusters = clusters
    return ContainerService(WorkloadService(settings, deployer, _NullBuilder()))


# --------------------------------------------------------------------------- #
# the stamp                                                                     #
# --------------------------------------------------------------------------- #


async def test_every_region_is_stamped_with_the_same_value():
    # A per-region value would leave the regions on different revisions.
    central, south = _FakeCluster("central", _ksvc()), _FakeCluster("south", _ksvc())
    svc = _service({"central": central, "south": south})

    await svc._engine.stamp_pull("app", "team", STAMP)

    for cluster in (central, south):
        _kind, name, body = cluster.patches[0]
        assert name == "app-team"
        assert body["metadata"]["annotations"][ANNOTATION_PULL_STAMP] == STAMP
        template = body["spec"]["template"]["metadata"]["annotations"]
        assert template[ANNOTATION_PULL_STAMP] == STAMP


async def test_the_patch_touches_nothing_but_the_two_annotations():
    # A merge patch, so anything it does not name is left as it is.
    central = _FakeCluster("central", _ksvc())
    svc = _service({"central": central})

    await svc._engine.stamp_pull("app", "team", STAMP)

    _kind, _name, body = central.patches[0]
    assert set(body) == {"metadata", "spec"}
    assert set(body["metadata"]) == {"annotations"}
    assert body["spec"] == {
        "template": {"metadata": {"annotations": {ANNOTATION_PULL_STAMP: STAMP}}}
    }


async def test_a_region_the_container_does_not_run_in_is_absent_not_a_failure():
    central, south = _FakeCluster("central", _ksvc()), _FakeCluster("south", None)
    svc = _service({"central": central, "south": south})

    statuses = await svc._engine.stamp_pull("app", "team", STAMP)

    assert {s.region: s.status for s in statuses} == {"central": "Deploying", "south": "Absent"}
    assert len(central.patches) == 1


# --------------------------------------------------------------------------- #
# accepting the request                                                         #
# --------------------------------------------------------------------------- #


async def test_pull_is_accepted_with_no_body_and_scheduled():
    svc = _service({"central": _FakeCluster("central", _ksvc())})
    background = BackgroundTasks()

    body = await svc.accept_pull("team", "app", USER, background)

    assert body.status == "Pending"
    assert body.statusUrl == "/api/v1/groups/team/containers/app"
    assert len(background.tasks) == 1


async def test_a_digest_pinned_container_is_refused():
    # A new revision would resolve the same digest, so there is nothing to pull.
    svc = _service({"central": _FakeCluster("central", _ksvc(image="reg/x@sha256:" + "a" * 64))})

    with pytest.raises(ValidationError, match="no newer image"):
        await svc.accept_pull("team", "app", USER, BackgroundTasks())


async def test_a_caller_outside_the_group_is_refused_before_anything_is_read():
    svc = _service({"central": _FakeCluster("central", _ksvc())})
    outsider = Principal(subject="u", username="bob", groups=["other"])

    with pytest.raises(ForbiddenError):
        await svc.accept_pull("team", "app", outsider, BackgroundTasks())


async def test_a_function_of_the_same_name_is_hidden():
    svc = _service({"central": _FakeCluster("central", _ksvc(offering="function"))})

    with pytest.raises(NotFoundError):
        await svc.accept_pull("team", "app", USER, BackgroundTasks())


async def test_the_background_work_stamps_a_fresh_value():
    central = _FakeCluster("central", _ksvc())
    svc = _service({"central": central})

    await svc.pull("team", "app")

    stamped = central.patches[0][2]["metadata"]["annotations"][ANNOTATION_PULL_STAMP]
    assert stamped.startswith("20")  # an ISO timestamp, minted at call time


# --------------------------------------------------------------------------- #
# carry-forward: an update must not cut a revision of its own                    #
# --------------------------------------------------------------------------- #


def _built(stamp):
    return build_ksvc(
        name="app-team",
        group="team",
        owner="alice",
        image="reg/x:1",
        offering="container",
        host="app-team.example.com",
        env=[],
        volumes=[],
        scaling=Scaling(),
        pull_stamp=stamp,
    )


def test_the_stamp_is_written_to_the_template_and_the_metadata():
    ksvc = _built(STAMP)

    # The template copy is what Knative diffs; the metadata copy is what an
    # update reads back.
    assert ksvc["spec"]["template"]["metadata"]["annotations"][ANNOTATION_PULL_STAMP] == STAMP
    assert ksvc["metadata"]["annotations"][ANNOTATION_PULL_STAMP] == STAMP


def test_a_container_that_was_never_pulled_carries_no_stamp():
    ksvc = _built(None)

    assert ANNOTATION_PULL_STAMP not in ksvc["spec"]["template"]["metadata"]["annotations"]
    assert ANNOTATION_PULL_STAMP not in ksvc["metadata"]["annotations"]


def test_the_stamp_survives_a_round_trip_through_read_back():
    # Dropped here, the next ordinary update would cut a revision nobody asked for.
    state = existing_state(_ksvc(stamp=STAMP), _FakeCluster("central", None), CONTAINER, "app-team")

    assert state["pull_stamp"] == STAMP
    assert _built(state["pull_stamp"]) == _built(STAMP)
