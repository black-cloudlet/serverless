import pytest

from app.auth.claims import principal_from_claims
from app.core.config import RHBKConfig, Settings, ZoneConfig
from app.core.errors import ValidationError, ZoneTotalFailure
from app.models.common import ZoneStatus
from app.services.deployer import Deployer, aggregate, status_code_for


def test_principal_from_claims_strips_and_detects_admin():
    cfg = RHBKConfig(groups_claim="groups", admin_groups=["platform-admins"])
    p = principal_from_claims(
        {"sub": "u1", "preferred_username": "alice", "groups": ["/team-a", "/platform-admins"]},
        cfg,
    )
    assert p.username == "alice"
    assert p.groups == ["team-a", "platform-admins"]
    assert p.is_admin is True
    assert p.can_access_group("anything") is True


def test_principal_non_admin_scope():
    cfg = RHBKConfig(admin_groups=[])
    p = principal_from_claims({"sub": "u", "groups": "team-a"}, cfg)
    assert p.groups == ["team-a"]
    assert p.can_access_group("team-a") is True
    assert p.can_access_group("team-b") is False


def test_aggregate_all_ok():
    statuses = [ZoneStatus(zone="a", status="Ready"), ZoneStatus(zone="b", status="Ready")]
    assert aggregate(statuses, "Ready") == "Ready"
    assert status_code_for("Ready", created=True) == 201


def test_aggregate_partial():
    statuses = [
        ZoneStatus(zone="a", status="Ready"),
        ZoneStatus(zone="b", status="Failed", error="boom"),
    ]
    assert aggregate(statuses, "Ready") == "Degraded"
    assert status_code_for("Degraded", created=True) == 207


def test_aggregate_total_failure():
    statuses = [ZoneStatus(zone="a", status="Failed", error="x")]
    with pytest.raises(ZoneTotalFailure):
        aggregate(statuses, "Ready")


def _settings_with_zones():
    return Settings(
        zones=[
            ZoneConfig(name="zone-a", api_server="https://a"),
            ZoneConfig(name="zone-b", api_server="https://b"),
        ]
    )


def test_resolve_targets_default_all():
    d = Deployer(_settings_with_zones())
    assert [z.name for z in d.resolve_targets(None)] == ["zone-a", "zone-b"]


def test_resolve_targets_unknown_zone():
    d = Deployer(_settings_with_zones())
    with pytest.raises(ValidationError):
        d.resolve_targets(["zone-c"])


async def test_fanout_captures_per_zone_errors():
    d = Deployer(_settings_with_zones())

    def fn(client, zone):
        if zone.name == "zone-b":
            raise RuntimeError("kaboom")
        return ZoneStatus(zone=zone.name, status="Ready")

    statuses = await d.fanout(d.resolve_targets(None), fn)
    by_zone = {s.zone: s for s in statuses}
    assert by_zone["zone-a"].status == "Ready"
    assert by_zone["zone-b"].error == "kaboom"
