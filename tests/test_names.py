"""The tenant-namespace naming rule: shape, length, grammar, reserved prefixes."""

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
    # Each half fitting does not make the pair fit: 63 minus the suffix is
    # the group's whole budget.
    budget = MAX_NAMESPACE_NAME - len(NAMESPACE_SUFFIX)
    assert namespace_for_group("g" * budget)  # at the cap: fine
    with pytest.raises(ValueError, match="shorten the group by 1"):
        namespace_for_group("g" * (budget + 1))


def test_the_error_names_the_arithmetic():
    with pytest.raises(ValueError, match=str(MAX_NAMESPACE_NAME)):
        namespace_for_group("g" * 64)


@pytest.mark.parametrize("group", ["", "-payments", "pay_ments", "Payments"])
def test_a_result_that_is_not_a_dns_label_is_rejected(group):
    # Defense-in-depth on the suffixed whole. (No trailing-hyphen case: the
    # suffix swallows it into a legal `group--serverless`.)
    with pytest.raises(ValueError):
        namespace_for_group(group)


def test_a_system_looking_namespace_is_refused():
    # `kube-team-serverless` would read as a system namespace.
    with pytest.raises(ValueError, match="reserved"):
        namespace_for_group("kube-team")
    with pytest.raises(ValueError, match="openshift-"):
        namespace_for_group("openshift-ops")


def test_an_ordinary_system_namespace_name_cannot_be_produced():
    # The suffix keeps any group from *equalling* an existing namespace.
    assert namespace_for_group("default") == "default-serverless"
