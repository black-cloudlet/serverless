"""The one ownership rule every read path shares."""

from cloudlet_apis.auth import Principal

from api.models.common import LABEL_GROUP, LABEL_OFFERING
from api.services.state.ownership import owned_by


def _ksvc(group="team", offering="container"):
    return {"metadata": {"labels": {LABEL_GROUP: group, LABEL_OFFERING: offering}}}


def _user(groups=("team",), admin=False):
    return Principal(subject="u", username="alice", groups=list(groups), is_admin=admin)


def test_owner_of_the_group_may_act_on_it():
    assert owned_by(_ksvc(), _user(), "container")


def test_another_groups_workload_is_not_owned():
    assert not owned_by(_ksvc(group="other"), _user(), "container")


def test_the_offering_is_checked_too():
    """The object name is {name}-{group} for both offerings.

    Without this half a container would be reachable through the functions path,
    since the name the API resolves is identical.
    """
    assert not owned_by(_ksvc(offering="function"), _user(), "container")


def test_an_unlabelled_object_is_never_owned():
    # A KSVC with no ownership labels is not ours; it must not read as public.
    assert not owned_by({}, _user(), "container")
    assert not owned_by({"metadata": {}}, _user(), "container")
    assert not owned_by({"metadata": {"labels": None}}, _user(), "container")


def test_an_admin_may_act_across_groups_but_not_across_offerings():
    admin = _user(groups=(), admin=True)
    assert owned_by(_ksvc(group="other"), admin, "container")
    # admin is about tenancy, not about reaching a container through /functions
    assert not owned_by(_ksvc(group="other", offering="function"), admin, "container")
