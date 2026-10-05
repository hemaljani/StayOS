"""Property and example tests for rule-definition validation and templates.

Covers rule-update validation acceptance semantics (Property 4) and the default
rule template seeding (Requirement 2.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.models import AlertTier, AlertType, RuleDefinition
from pulse.rule_engine.rule_validation import (
    ESCALATION_TIMEOUT_MAX_SEC,
    ESCALATION_TIMEOUT_MIN_SEC,
    REQUIRED_ATTRIBUTES,
    default_rule_templates,
    validate_rule_update,
)

PROPERTY_SETTINGS = settings(max_examples=200)

_VALID_TIERS = [t.value for t in AlertTier]
_VALID_TYPES = [t.value for t in AlertType]


def _expected_accept(update: Mapping[str, Any]) -> bool:
    """Independent oracle mirroring the validation contract.

    Args:
        update: The candidate update.

    Returns:
        Whether the update should be accepted.
    """
    for attribute in REQUIRED_ATTRIBUTES:
        if update.get(attribute) is None:
            return False
    if update["ruleType"] not in _VALID_TYPES:
        return False
    if update["tier"] not in _VALID_TIERS:
        return False
    for flag in ("agentTriageEnabled", "enabled"):
        if not isinstance(update[flag], bool):
            return False
    if not isinstance(update["triggerCondition"], Mapping):
        return False
    timeout = update["escalationTimeoutSec"]
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return False
    if not (ESCALATION_TIMEOUT_MIN_SEC <= timeout <= ESCALATION_TIMEOUT_MAX_SEC):
        return False
    if not isinstance(update.get("parameters", {}), Mapping):
        return False
    return True


# A flag strategy that mixes real booleans with invalid non-boolean values.
_flag_strategy = st.sampled_from([True, False, "yes", 0, 1])


@st.composite
def _rule_updates(draw: st.DrawFn) -> dict[str, Any]:
    """Generate rule-definition updates spanning valid and invalid values."""
    update: dict[str, Any] = {
        "propertyId": "ALOHA-CHI-001",
        "ruleType": draw(st.sampled_from(_VALID_TYPES + ["BOGUS_TYPE"])),
        "tier": draw(st.sampled_from(_VALID_TIERS + ["BOGUS_TIER"])),
        "triggerCondition": {"operator": "eq", "left": 1, "right": 1},
        "agentTriageEnabled": draw(_flag_strategy),
        "enabled": draw(_flag_strategy),
        "escalationTimeoutSec": draw(st.integers(min_value=-100, max_value=100000)),
        "parameters": {},
    }
    # Occasionally drop one required attribute to exercise presence rejection.
    if draw(st.booleans()):
        victim = draw(st.sampled_from(list(REQUIRED_ATTRIBUTES)))
        update.pop(victim, None)
    return update


# Feature: initial-pulse-project, Property 4: Rule-update validation accepts
# exactly in-range definitions
@PROPERTY_SETTINGS
@given(update=_rule_updates())
def test_property_4_validation_accepts_exactly_in_range(
    update: dict[str, Any],
) -> None:
    """Accept iff every attribute is present-when-required and in range.

    Validates: Requirements 2.5, 2.6, 6.4
    """
    prior = RuleDefinition(
        property_id="ALOHA-CHI-001",
        rule_type=AlertType.WALK_RISK,
        tier=AlertTier.CRITICAL,
        trigger_condition={"operator": "eq", "left": 1, "right": 1},
        agent_triage_enabled=True,
        escalation_timeout_sec=300,
        enabled=True,
    )

    result = validate_rule_update(update, prior)
    expected = _expected_accept(update)

    assert result.accepted == expected
    if result.accepted:
        assert result.rule is not None
        assert result.rule is not prior
        assert result.invalid_attribute is None
        assert ESCALATION_TIMEOUT_MIN_SEC <= result.rule.escalation_timeout_sec
        assert result.rule.escalation_timeout_sec <= ESCALATION_TIMEOUT_MAX_SEC
    else:
        # On rejection the prior definition is retained unchanged and the
        # offending attribute is identified.
        assert result.rule is prior
        assert result.invalid_attribute is not None


def test_boundary_timeouts_accepted() -> None:
    """The inclusive escalation-timeout boundaries are accepted."""
    for timeout in (ESCALATION_TIMEOUT_MIN_SEC, ESCALATION_TIMEOUT_MAX_SEC):
        update = {
            "propertyId": "P",
            "ruleType": AlertType.WALK_RISK.value,
            "tier": AlertTier.CRITICAL.value,
            "triggerCondition": {"operator": "gt", "left": "a", "right": "b"},
            "agentTriageEnabled": True,
            "enabled": True,
            "escalationTimeoutSec": timeout,
        }
        assert validate_rule_update(update).accepted is True


def test_just_out_of_range_timeouts_rejected() -> None:
    """Timeouts one step outside the inclusive range are rejected."""
    for timeout in (ESCALATION_TIMEOUT_MIN_SEC - 1, ESCALATION_TIMEOUT_MAX_SEC + 1):
        update = {
            "propertyId": "P",
            "ruleType": AlertType.WALK_RISK.value,
            "tier": AlertTier.CRITICAL.value,
            "triggerCondition": {"operator": "gt", "left": "a", "right": "b"},
            "agentTriageEnabled": True,
            "enabled": True,
            "escalationTimeoutSec": timeout,
        }
        result = validate_rule_update(update)
        assert result.accepted is False
        assert result.invalid_attribute == "escalationTimeoutSec"


def test_default_templates_all_enabled_one_per_type() -> None:
    """Default templates cover every rule-engine alert type, all enabled (Req 2.2)."""
    templates = default_rule_templates("ALOHA-CHI-001")

    # FORECAST_OVERSELL is advisory: it is written directly by the forecasting
    # agent, not produced by the rule engine, so it intentionally has no default
    # rule template. Exclude it from the "one template per type" cover check.
    rule_engine_types = set(AlertType) - {AlertType.FORECAST_OVERSELL}
    assert {t.rule_type for t in templates} == rule_engine_types
    assert all(t.enabled for t in templates)
    # Every seeded template enables triage: CRITICAL/WARNING get an action brief
    # and the two INFO types get an advisory opportunity brief.
    for template in templates:
        assert template.agent_triage_enabled is True
        assert (
            ESCALATION_TIMEOUT_MIN_SEC
            <= template.escalation_timeout_sec
            <= ESCALATION_TIMEOUT_MAX_SEC
        )
