"""Property and unit tests for the escalation-trigger hierarchy.

Covers Property 9 (the recorded reasons equal exactly the satisfied conditions
and mandatory review fires iff at least one holds) and the Requirement 11.10
fail-safe (threshold-unavailable).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.models import EscalationReason, EscalationStatus
from pulse.escalation.triggers import (
    CONFIDENCE_REVIEW_THRESHOLD,
    ETA_REVIEW_THRESHOLD_MIN,
    REPEAT_ISSUE_MIN_OCCURRENCES,
    AlertSignals,
    BrandPolicy,
    evaluate_escalation_triggers,
)

PROPERTY_SETTINGS = settings(max_examples=200)

_AMBASSADOR_TIERS = frozenset({"Ambassador"})
_TIER_CHOICES = [None, "Gold", "Platinum", "Ambassador"]


def _expected_reasons(
    signals: AlertSignals, policy: BrandPolicy
) -> set[EscalationReason]:
    """Independent oracle computing the reasons that should fire.

    Mirrors Requirement 11 directly rather than reusing production logic, so the
    property test cross-checks the implementation against the spec.
    """
    expected: set[EscalationReason] = set()
    if signals.legal_safety_language:
        expected.add(EscalationReason.LEGAL_SAFETY)
    if signals.vip_tier is not None and signals.vip_tier in policy.ambassador_tiers:
        expected.add(EscalationReason.VIP_AMBASSADOR)
    if not policy.threshold_available:
        expected.add(EscalationReason.THRESHOLD_UNAVAILABLE)
    elif (
        signals.dollar_value is not None
        and policy.dollar_threshold is not None
        and signals.dollar_value >= policy.dollar_threshold
    ):
        expected.add(EscalationReason.DOLLAR_THRESHOLD)
    if (
        signals.confidence is not None
        and signals.confidence < CONFIDENCE_REVIEW_THRESHOLD
    ):
        expected.add(EscalationReason.LOW_CONFIDENCE)
    if signals.repeat_issue_count >= REPEAT_ISSUE_MIN_OCCURRENCES:
        expected.add(EscalationReason.REPEAT_ISSUE)
    if signals.sla_breached or (
        signals.eta_minutes is not None
        and signals.eta_minutes < ETA_REVIEW_THRESHOLD_MIN
    ):
        expected.add(EscalationReason.SLA_OR_ETA)
    if signals.manager_requested:
        expected.add(EscalationReason.MANAGER_REQUESTED)
    return expected


# Feature: initial-pulse-project, Property 9: Escalation-trigger hierarchy
# records exactly the satisfied reasons
@PROPERTY_SETTINGS
@given(
    legal_safety_language=st.booleans(),
    vip_tier=st.sampled_from(_TIER_CHOICES),
    dollar_value=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1_000_000.0)),
    confidence=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
    repeat_issue_count=st.integers(min_value=0, max_value=5),
    sla_breached=st.booleans(),
    eta_minutes=st.one_of(st.none(), st.floats(min_value=0.0, max_value=240.0)),
    manager_requested=st.booleans(),
    dollar_threshold=st.floats(min_value=0.01, max_value=500_000.0),
    threshold_available=st.booleans(),
)
def test_property_9_reasons_equal_satisfied_conditions(
    legal_safety_language: bool,
    vip_tier: str | None,
    dollar_value: float | None,
    confidence: int | None,
    repeat_issue_count: int,
    sla_breached: bool,
    eta_minutes: float | None,
    manager_requested: bool,
    dollar_threshold: float,
    threshold_available: bool,
) -> None:
    """Reasons recorded equal exactly the satisfied set; status iff non-empty.

    Validates: Requirements 4.3, 5.3, 7.5, 10.4, 11.1, 11.2, 11.3, 11.4, 11.5,
    11.6, 11.7, 11.9
    """
    signals = AlertSignals(
        legal_safety_language=legal_safety_language,
        vip_tier=vip_tier,
        dollar_value=dollar_value,
        confidence=confidence,
        repeat_issue_count=repeat_issue_count,
        sla_breached=sla_breached,
        eta_minutes=eta_minutes,
        manager_requested=manager_requested,
    )
    policy = BrandPolicy(
        dollar_threshold=dollar_threshold,
        threshold_available=threshold_available,
        ambassador_tiers=_AMBASSADOR_TIERS,
    )

    decision = evaluate_escalation_triggers(signals, policy)
    expected = _expected_reasons(signals, policy)

    # Exact set equality (Property 9) and no duplicates (single queue entry).
    assert set(decision.reasons) == expected
    assert len(decision.reasons) == len(set(decision.reasons))

    # Mandatory review iff at least one reason fired.
    if expected:
        assert decision.escalation_status is EscalationStatus.MANDATORY_GM_REVIEW
    else:
        assert decision.escalation_status is EscalationStatus.NONE


def test_no_triggers_yields_no_review() -> None:
    """An alert with no satisfied trigger is not flagged for review."""
    decision = evaluate_escalation_triggers(
        AlertSignals(), BrandPolicy(dollar_threshold=100.0)
    )
    assert decision.escalation_status is EscalationStatus.NONE
    assert decision.reasons == []


def test_requirement_11_10_threshold_unavailable_fail_safe() -> None:
    """An unretrievable brand threshold escalates via threshold-unavailable.

    Validates: Requirement 11.10
    """
    decision = evaluate_escalation_triggers(
        AlertSignals(dollar_value=5000.0),
        BrandPolicy(dollar_threshold=None, threshold_available=False),
    )
    assert decision.escalation_status is EscalationStatus.MANDATORY_GM_REVIEW
    assert EscalationReason.THRESHOLD_UNAVAILABLE in decision.reasons
    # The dollar-threshold reason must NOT be recorded when the threshold is
    # unavailable (it could not be evaluated).
    assert EscalationReason.DOLLAR_THRESHOLD not in decision.reasons


def test_multiple_triggers_single_entry_all_reasons() -> None:
    """Multiple satisfied triggers are recorded on one decision (Req 11.9)."""
    decision = evaluate_escalation_triggers(
        AlertSignals(
            legal_safety_language=True,
            vip_tier="Ambassador",
            manager_requested=True,
        ),
        BrandPolicy(dollar_threshold=100.0, ambassador_tiers=_AMBASSADOR_TIERS),
    )
    assert decision.escalation_status is EscalationStatus.MANDATORY_GM_REVIEW
    assert set(decision.reasons) == {
        EscalationReason.LEGAL_SAFETY,
        EscalationReason.VIP_AMBASSADOR,
        EscalationReason.MANAGER_REQUESTED,
    }
