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


def test_the_chart_emits_exactly_the_placeholders_the_controller_substitutes():
    """A token the code does not know fails the whole set at load - in the
    backoff log, with nothing converged - so catch it against the chart instead.

    The ConfigMap never writes a token literally; every one comes from a helper,
    so the helpers are the list to compare against the code.
    """
    helpers = (TEMPLATES / "_helpers.tpl").read_text()
    emitted = {
        line.split("`")[1].strip("{}")
        for line in helpers.splitlines()
        if "Token" in line and "`{{" in line
    }
    assert emitted == set(PLACEHOLDERS), (
        f"the chart emits {sorted(emitted)}; the tenant controller substitutes {sorted(PLACEHOLDERS)}"
    )
