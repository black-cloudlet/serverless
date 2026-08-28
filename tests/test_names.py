"""The tenant-namespace naming rule (docs/proposals/namespace-per-group.md).

``namespace_for_group`` is the one mapping the API (deploy target), the
provisioner (create/converge) and the GC (delete) must agree on, so its edges
are pinned here: the ``{group}-serverless`` shape, the 63-character DNS-label
cap on the *suffixed* result, the label grammar, and the reserved-prefix
refusal that group-first naming makes necessary.
"""

from __future__ import annotations

import pytest

from common.names import (
    MAX_NAMESPACE_NAME,
    NAMESPACE_SUFFIX,
    namespace_for_group,
)


def test_the_namespace_is_the_suffixed_group():
    assert namespace_for_group("payments") == "payments-serverless"


def test_the_suffix_is_configurable():
    # The chart's value flows through; the rule stays the same rule.
    assert namespace_for_group("payments", suffix="-acme") == "payments-acme"


def test_a_group_valid_alone_can_still_be_too_long_suffixed():
    # Exactly the {name}-{group} lesson: each half fitting does not make the
    # pair fit. 63 minus the suffix is the group's whole budget.
    budget = MAX_NAMESPACE_NAME - len(NAMESPACE_SUFFIX)
    assert namespace_for_group("g" * budget)  # at the cap: fine
    with pytest.raises(ValueError, match="shorten the group by 1"):
        namespace_for_group("g" * (budget + 1))


def test_the_error_names_the_arithmetic():
    with pytest.raises(ValueError, match=str(MAX_NAMESPACE_NAME)):
        namespace_for_group("g" * 64)


@pytest.mark.parametrize("group", ["", "-payments", "pay_ments", "Payments"])
def test_a_result_that_is_not_a_dns_label_is_rejected(group):
    # The group arrives normalized, so these are defense-in-depth: the check
    # is the namespace's own rule on the *suffixed* result, not a second
    # normalization pass. (A trailing hyphen is absent here: the suffix
    # swallows it into a legal `group--serverless`.)
    with pytest.raises(ValueError):
        namespace_for_group(group)


def test_a_system_looking_namespace_is_refused():
    # Group-first naming means a group beginning with a reserved system
    # prefix would produce a namespace that reads as the system's own -
    # `kube-team-serverless` - which the platform refuses to create.
    with pytest.raises(ValueError, match="reserved system prefix"):
        namespace_for_group("kube-team")
    with pytest.raises(ValueError, match="openshift-"):
        namespace_for_group("openshift-ops")


def test_an_ordinary_system_namespace_name_cannot_be_produced():
    # The suffix removes the head-on collision class: whatever the group, the
    # result never *equals* an existing cluster namespace's name.
    assert namespace_for_group("default") == "default-serverless"
