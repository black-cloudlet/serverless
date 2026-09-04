"""The seams between the chart and the code that consumes what it renders.

Three of these are literals the chart cannot import: the namespace suffix, the
ownership label the Kyverno policy matches tenant namespaces on, and the kinds
the tenant controller's ClusterRole must cover. Each is correct today by agreement,
which is the kind of agreement that quietly stops holding - and none of it
fails visibly. A missing verb fails every converge in the backoff log; a stale
label just stops injecting the CA into build pods, and the first anyone hears
is a build that cannot reach an internal git server.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from common.labels import LABEL_MANAGED_BY, TENANT_CONTROLLER_VALUE
from common.names import NAMESPACE_SUFFIX
from tenant_controller.templates import PLACEHOLDERS, TEMPLATE_KINDS

CHART = Path(__file__).resolve().parent.parent / "charts" / "serverless-api"
TEMPLATES = CHART / "templates"


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def _plural(kind: str) -> str:
    """The API's plural for a kind, as an RBAC rule spells it."""
    lower = kind.lower()
    return f"{lower[:-1]}ies" if lower.endswith("y") else f"{lower}s"


def test_the_charts_namespace_suffix_is_the_one_the_code_derives():
    """Both sides build `{group}{suffix}`; a mismatch strands every namespace."""
    assert _values()["tenantNamespaces"]["suffix"] == NAMESPACE_SUFFIX


def test_the_ca_policy_matches_the_label_the_controller_actually_stamps():
    """Kyverno cannot list tenant namespaces by name - they are made at runtime -
    so it matches this label. It is a literal in the chart and a constant in the
    code, and only this test connects them."""
    policy = (TEMPLATES / "kpack" / "ca-policy.yaml").read_text()
    assert f"{LABEL_MANAGED_BY}: {TENANT_CONTROLLER_VALUE}" in policy


def test_the_tenant_controller_may_write_every_kind_its_templates_can_render():
    """A kind the set can render but the ClusterRole cannot write fails every
    converge; one it can write but not delete can never be pruned."""
    rbac = (TEMPLATES / "tenant-controller" / "rbac.yaml").read_text()
    for kind in TEMPLATE_KINDS:
        assert _plural(kind.kind) in rbac, (
            f"{kind.kind} is in TEMPLATE_KINDS but the tenant controller ClusterRole "
            f"never names {_plural(kind.kind)}"
        )


def test_the_tenant_controller_writes_namespaces_and_the_api_does_not():
    """The separation the whole component exists for, asserted rather than assumed."""
    rbac = (TEMPLATES / "tenant-controller" / "rbac.yaml").read_text()
    api_rbac = (TEMPLATES / "rbac.yaml").read_text()
    assert '"namespaces"' in rbac and "create" in rbac
    # The API reads namespaces cluster-wide (the host pre-flight) but must never
    # be granted a verb that changes one.
    read_only = api_rbac.split("name: {{ .Values.name }}-read")[1]
    assert '"namespaces"' in read_only
    for verb in ('"create"', '"delete"', '"patch"', '"update"'):
        assert verb not in read_only, f"the API's cluster-wide role must not hold {verb}"


def test_the_two_services_hold_different_identities():
    """One cert, one username: sharing it would erase the split above."""
    values = _values()
    assert (
        values["tenantNamespaces"]["controller"]["certificate"]["user"]
        != values["certificate"]["user"]
    )


def test_every_part_the_controller_mounts_has_a_configmap_to_mount():
    """The parts list is the join between the Deployment and these files.

    A part with no file is a ConfigMap the pod cannot mount, so it never
    starts; a file with no part entry is a resource group that renders, passes
    review, and is silently never applied to a tenant. CI catches the first
    direction end to end; this catches the second, which is the quiet one.
    """
    helper = (TEMPLATES / "tenant-controller" / "_tenant.tpl").read_text()
    body = helper.split('define "serverless-api.tenantTemplateParts" -}}')[1].split("{{- end -}}")[
        0
    ]
    listed = set(re.sub(r"\{\{.*?\}\}", " ", body).split())
    on_disk = {
        path.name.removeprefix("configmap-").removesuffix(".yaml")
        for path in (TEMPLATES / "tenant-controller").glob("configmap-*.yaml")
    }
    assert listed == on_disk, (
        f"the Deployment mounts {sorted(listed)}; the chart renders {sorted(on_disk)}"
    )


def test_the_chart_emits_exactly_the_placeholders_the_controller_substitutes():
    """A token the code does not know fails the whole set at load - in the
    backoff log, with nothing converged - so catch it against the chart instead.

    No ConfigMap writes a token literally; every one comes from a helper, so
    the helpers are the list to compare against the code.
    """
    helpers = (TEMPLATES / "tenant-controller" / "_tenant.tpl").read_text()
    emitted = {
        line.split("`")[1].strip("{}")
        for line in helpers.splitlines()
        if "Token" in line and "`{{" in line
    }
    assert emitted == set(PLACEHOLDERS), (
        f"the chart emits {sorted(emitted)}; the tenant controller substitutes {sorted(PLACEHOLDERS)}"
    )


def test_the_provision_endpoints_policy_is_not_governed_by_the_workload_switch():
    """`networkPolicy.enabled` governs tenant namespaces, never this endpoint.

    The switch reads as being about workload pods, so an operator turning it
    off to debug connectivity - or on a CNI that does not enforce policy -
    expects to change what a tenant namespace gets. Letting it also drop the
    policy in front of the provision endpoint would take away that endpoint's
    primary control, leaving only a shared token that is off by default.
    """
    deployment = (TEMPLATES / "tenant-controller" / "deployment.yaml").read_text()
    policy = deployment.index("kind: NetworkPolicy")
    guards = re.findall(r"\{\{-?\s*if\s+([^}]+?)\s*-?\}\}", deployment[:policy])
    opened = [g for g in guards if "networkPolicy.enabled" in g]
    assert not opened, f"the provision endpoint's NetworkPolicy sits under {opened}"


def test_the_workload_policies_render_and_mount_together():
    """The part is listed exactly when its ConfigMap renders.

    Listed without rendering, the pod mounts a ConfigMap that does not exist
    and never starts; rendered without being listed, the policies are applied
    to no tenant. Both sides carry the same condition, so they move together.
    """
    helper = (TEMPLATES / "tenant-controller" / "_tenant.tpl").read_text()
    parts = helper.split('define "serverless-api.tenantTemplateParts" -}}')[1]
    parts = parts.split("{{- end -}}")[0]
    listing = next(line for line in parts.splitlines() if "network-policies" in line)
    configmap = (TEMPLATES / "tenant-controller" / "configmap-network-policies.yaml").read_text()
    assert "networkPolicy.enabled" in listing, listing
    assert "networkPolicy.enabled" in configmap.splitlines()[0], configmap.splitlines()[0]
    assert configmap.rstrip().endswith("{{- end }}")


def test_the_backup_part_renders_and_mounts_together():
    """`backup.enabled` governs both sides, like `networkPolicy.enabled`.

    Listed without rendering, the pod mounts a ConfigMap that does not exist
    and never starts; rendered without being listed, no tenant namespace is
    ever backed up - and nothing about a namespace looks different until
    someone needs a restore.
    """
    helper = (TEMPLATES / "tenant-controller" / "_tenant.tpl").read_text()
    parts = helper.split('define "serverless-api.tenantTemplateParts" -}}')[1]
    parts = parts.split("{{- end -}}")[0]
    listing = next(line for line in parts.splitlines() if "backup" in line)
    configmap = (TEMPLATES / "tenant-controller" / "configmap-backup.yaml").read_text()
    assert "backup.enabled" in listing, listing
    assert "backup.enabled" in configmap.splitlines()[0], configmap.splitlines()[0]
    assert configmap.rstrip().endswith("{{- end }}")


def test_the_backup_application_is_named_for_the_namespace_and_the_region():
    """One application per namespace per region, `{namespace}-{region}`.

    The same tenant namespace exists in both clusters and is backed up in both.
    Naming the application after the namespace alone would give the two copies
    one name wherever they are listed together - a shared AppVault, a restore -
    and the region token is also what keeps the set region-neutral while its
    output is not.
    """
    configmap = (TEMPLATES / "tenant-controller" / "configmap-backup.yaml").read_text()
    assert 'printf "%s-%s" $ns $region' in configmap
    for token in ("tenantNamespaceToken", "tenantRegionToken"):
        assert token in configmap, f"the backup part never resolves {token}"


def test_the_default_schedules_cover_an_hour_a_day_and_a_week():
    """The retention ladder is the feature, so it is asserted rather than read.

    Each granularity carries the time fields Trident Protect requires for it -
    a Weekly schedule with no `dayOfWeek` never fires, and nothing in the
    cluster says so.
    """
    required = {
        "Hourly": {"minute"},
        "Daily": {"minute", "hour"},
        "Weekly": {"minute", "hour", "dayOfWeek"},
        "Monthly": {"minute", "hour", "dayOfMonth"},
    }
    schedules = {s["name"]: s for s in _values()["backup"]["schedules"]}
    assert set(schedules) == {"hourly", "daily", "weekly"}
    assert schedules["hourly"]["granularity"] == "Hourly"
    assert schedules["daily"]["granularity"] == "Daily"
    assert schedules["weekly"]["granularity"] == "Weekly"
    assert schedules["hourly"]["backupRetention"] == "2"
    assert schedules["daily"]["backupRetention"] == "2"
    assert schedules["weekly"]["backupRetention"] == "1"
    for name, schedule in schedules.items():
        missing = required[schedule["granularity"]] - set(schedule)
        assert not missing, f"the {name} schedule is missing {sorted(missing)}"
        # Strings, because the CRD's fields are strings - and because Helm
        # renders a large enough number in scientific notation (test_chart_values).
        for field in required[schedule["granularity"]] | {"backupRetention"}:
            assert isinstance(schedule[field], str), f"{name}.{field} must be quoted"


def test_backups_are_off_until_an_operator_names_an_appvault():
    """Both halves of the same decision: the chart cannot supply either
    prerequisite - the CRDs on the cluster and an AppVault holding the object
    store's credentials - so a default install renders no Schedule at all,
    rather than one that writes nowhere."""
    backup = _values()["backup"]
    assert backup["enabled"] is False
    assert backup["appVault"]["name"] == ""
    helpers = (TEMPLATES / "_helpers.tpl").read_text()
    assert "backup.appVault.name is required" in helpers


def test_nothing_of_the_template_set_renders_without_the_controller():
    """The set exists only to be mounted by the controller's pod.

    Its parts are ConfigMaps in the API namespace, so a release with the
    controller off would otherwise leave them behind with nothing to read
    them - and a later reader could take them for live configuration.
    """
    parts = sorted((TEMPLATES / "tenant-controller").glob("configmap-*.yaml"))
    assert parts, "no template-set ConfigMaps found"
    for part in parts:
        first = part.read_text().splitlines()[0]
        assert "tenantNamespaces.controller.enabled" in first, f"{part.name}: {first}"
