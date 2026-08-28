"""The tenant-namespace naming rule (docs/proposals/namespace-per-group.md).

``namespace_for_group`` is the one mapping the API (deploy target), the
provisioner (create/converge) and the GC (delete) must agree on, so its edges
are pinned here: the prefix, the 63-character DNS-label cap on the *prefixed*
result, and the label grammar.
"""

from __future__ import annotations

import pytest

from common.names import (
    MAX_NAMESPACE_NAME,
    NAMESPACE_PREFIX,
    namespace_for_group,
)


def test_the_namespace_is_the_prefixed_group():
    assert namespace_for_group("payments") == "serverless-t-payments"


def test_the_prefix_is_configurable():
    # The chart's value flows through; the rule stays the same rule.
    assert namespace_for_group("payments", prefix="acme-") == "acme-payments"


def test_a_group_valid_alone_can_still_be_too_long_prefixed():
    # Exactly the {name}-{group} lesson: each half fitting does not make the
    # pair fit. 63 minus the prefix is the group's whole budget.
    budget = MAX_NAMESPACE_NAME - len(NAMESPACE_PREFIX)
    assert namespace_for_group("g" * budget)  # at the cap: fine
    with pytest.raises(ValueError, match="shorten the group by 1"):
        namespace_for_group("g" * (budget + 1))


def test_the_error_names_the_arithmetic():
    with pytest.raises(ValueError, match=str(MAX_NAMESPACE_NAME)):
        namespace_for_group("g" * 64)


@pytest.mark.parametrize("group", ["", "payments-", "pay_ments", "Payments"])
def test_a_result_that_is_not_a_dns_label_is_rejected(group):
    # The group arrives normalized, so these are defense-in-depth: the check
    # is the namespace's own rule on the *prefixed* result, not a second
    # normalization pass. (Which is why a leading hyphen is absent here: the
    # prefix swallows it into a legal `t--group`.)
    with pytest.raises(ValueError):
        namespace_for_group(group)


def test_a_system_namespace_name_cannot_be_produced():
    # The prefix removes the collision class between a group name and an
    # existing cluster namespace: whatever the group, the result starts with
    # the platform's own prefix.
    assert namespace_for_group("kube-system").startswith(NAMESPACE_PREFIX)
    assert namespace_for_group("default") == f"{NAMESPACE_PREFIX}default"
