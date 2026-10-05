"""Escalation-trigger hierarchy: mandatory GM review decision (pure logic).

This module implements Requirement 11 (and the tier-specific triggers 4.3, 5.3,
7.5, 10.4): a single **pure** function maps an alert's escalation-relevant
signals plus the brand policy to the exact set of :class:`EscalationReason`
values that fire, and hence whether the alert must be surfaced for mandatory GM
review.

The function is deliberately I/O-free so it is unit-testable without a Lambda,
DynamoDB, or SPOG dependency. The caller is responsible for gathering the
signals (from the alert, its triage brief, and SPOG) and for persisting the
resulting :class:`EscalationDecision` and surfacing the alert to the review
queue within the latency budget (Requirement 11.8).

Semantics enforced here:
    * The escalation status is ``MANDATORY_GM_REVIEW`` if and only if at least
      one trigger reason fires; otherwise ``NONE`` (Requirement 11, Property 9).
    * The recorded reasons equal *exactly* the set of satisfied conditions, with
      no duplicates, returned in a deterministic canonical order (Property 9).
    * When more than one trigger fires the alert is a single decision carrying
      all reasons (one queue entry, Requirement 11.9).
    * Fail-safe: if the brand-policy dollar threshold cannot be retrieved, the
      ``threshold-unavailable`` reason fires and the alert is escalated rather
      than silently skipping the dollar check (Requirement 11.10).

Contract-penalty risk (Requirement 7.5) is modeled as a financial value: the
caller passes the penalty amount as ``dollar_value`` so it is evaluated by the
``dollar-threshold`` trigger, consistent with the consolidated hierarchy in
design Property 9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pulse.common.logging import get_logger
from pulse.common.models import (
    EscalationDecision,
    EscalationReason,
    EscalationStatus,
)

logger = get_logger("pulse-escalation-service")

# Confidence at or above this percentage does NOT trigger low-confidence review;
# strictly below it does (Requirements 10.4, 11.4).
CONFIDENCE_REVIEW_THRESHOLD = 85

# A guest ETA strictly below this many minutes triggers sla-or-eta review
# (Requirement 11.6).
ETA_REVIEW_THRESHOLD_MIN = 45

# The same issue for the same room occurring this many times or more within the
# rolling window triggers repeat-issue review (Requirement 11.5).
REPEAT_ISSUE_MIN_OCCURRENCES = 2

# Default VIP tiers considered "Ambassador or above" (Requirements 4.3, 11.2).
# A property/brand may extend this with additional top tiers.
DEFAULT_AMBASSADOR_TIERS = frozenset({"Ambassador"})

# Canonical ordering used when returning the satisfied reasons, so the recorded
# reason list is deterministic (set semantics per Property 9, stable order for
# readability and stable assertions).
_REASON_ORDER: tuple[EscalationReason, ...] = (
    EscalationReason.LEGAL_SAFETY,
    EscalationReason.VIP_AMBASSADOR,
    EscalationReason.DOLLAR_THRESHOLD,
    EscalationReason.LOW_CONFIDENCE,
    EscalationReason.REPEAT_ISSUE,
    EscalationReason.SLA_OR_ETA,
    EscalationReason.MANAGER_REQUESTED,
    EscalationReason.THRESHOLD_UNAVAILABLE,
)


@dataclass(frozen=True)
class BrandPolicy:
    """Brand-policy inputs required to evaluate the escalation hierarchy.

    Attributes:
        dollar_threshold: The currency value at or above which an alert's dollar
            value triggers mandatory review (Requirement 11.3, a value between
            0.01 and 999,999,999.99). Ignored when ``threshold_available`` is
            ``False``.
        threshold_available: Whether the dollar threshold could be retrieved.
            When ``False`` the fail-safe ``threshold-unavailable`` reason fires
            (Requirement 11.10).
        ambassador_tiers: The set of VIP tiers treated as "Ambassador or above"
            for the vip-ambassador trigger (Requirements 4.3, 11.2).
    """

    dollar_threshold: Optional[float] = None
    threshold_available: bool = True
    ambassador_tiers: frozenset[str] = DEFAULT_AMBASSADOR_TIERS


@dataclass(frozen=True)
class AlertSignals:
    """The escalation-relevant signals gathered for a single alert.

    Each field maps to one trigger condition in Requirement 11. Fields default
    to the non-triggering value so a caller only sets the signals relevant to
    the alert type it is evaluating.

    Attributes:
        legal_safety_language: Whether legal or safety language was detected in
            the alert content (Requirements 5.3, 11.1).
        vip_tier: The guest VIP tier, if any (Requirements 4.3, 11.2).
        dollar_value: The alert's financial value or contract-penalty amount, if
            any (Requirements 7.5, 11.3).
        confidence: The triage confidence percentage 0-100, if a triage brief
            exists (Requirements 10.4, 11.4).
        repeat_issue_count: Occurrences of the same issue for the same room
            within the rolling 30-day window (Requirement 11.5).
        sla_breached: Whether an SLA is already breached (Requirement 11.6).
        eta_minutes: Guest ETA in minutes, if known (Requirement 11.6).
        manager_requested: Whether the guest explicitly requested a manager
            (Requirement 11.7).
    """

    legal_safety_language: bool = False
    vip_tier: Optional[str] = None
    dollar_value: Optional[float] = None
    confidence: Optional[int] = None
    repeat_issue_count: int = 0
    sla_breached: bool = False
    eta_minutes: Optional[float] = None
    manager_requested: bool = False


def _dollar_reasons(
    signals: AlertSignals, policy: BrandPolicy
) -> list[EscalationReason]:
    """Return the dollar-related reasons (threshold or fail-safe).

    Args:
        signals: The alert signals (for the dollar value).
        policy: The brand policy (threshold and its availability).

    Returns:
        ``[THRESHOLD_UNAVAILABLE]`` when the threshold cannot be retrieved,
        ``[DOLLAR_THRESHOLD]`` when the dollar value meets an available
        threshold, or an empty list otherwise.
    """
    # Requirement 11.10 fail-safe: an unavailable threshold escalates rather
    # than silently skipping the dollar check.
    if not policy.threshold_available:
        return [EscalationReason.THRESHOLD_UNAVAILABLE]
    if (
        signals.dollar_value is not None
        and policy.dollar_threshold is not None
        and signals.dollar_value >= policy.dollar_threshold
    ):
        return [EscalationReason.DOLLAR_THRESHOLD]
    return []


def evaluate_escalation_triggers(
    signals: AlertSignals, policy: BrandPolicy
) -> EscalationDecision:
    """Evaluate the escalation-trigger hierarchy for one alert (pure).

    Applies every Requirement 11 trigger to the supplied signals and brand
    policy and returns the exact set of satisfied reasons. The escalation status
    is ``MANDATORY_GM_REVIEW`` if and only if at least one reason fires
    (Property 9). When several triggers fire, all reasons are recorded on the
    single returned decision (one queue entry, Requirement 11.9).

    Args:
        signals: The alert's escalation-relevant signals.
        policy: The brand policy (dollar threshold and its availability, VIP
            ambassador tiers).

    Returns:
        An :class:`EscalationDecision` whose ``reasons`` is the exact,
        deduplicated set of satisfied trigger reasons in canonical order, and
        whose ``escalation_status`` reflects whether any reason fired.
    """
    fired: set[EscalationReason] = set()

    # 11.1 / 5.3 - legal or safety language.
    if signals.legal_safety_language:
        fired.add(EscalationReason.LEGAL_SAFETY)

    # 11.2 / 4.3 - VIP tier Ambassador or above.
    if signals.vip_tier is not None and signals.vip_tier in policy.ambassador_tiers:
        fired.add(EscalationReason.VIP_AMBASSADOR)

    # 11.3 / 7.5 - dollar value at/above the brand threshold, or 11.10 fail-safe.
    fired.update(_dollar_reasons(signals, policy))

    # 11.4 / 10.4 - triage confidence below the review threshold.
    if (
        signals.confidence is not None
        and signals.confidence < CONFIDENCE_REVIEW_THRESHOLD
    ):
        fired.add(EscalationReason.LOW_CONFIDENCE)

    # 11.5 - repeat issue within the rolling window.
    if signals.repeat_issue_count >= REPEAT_ISSUE_MIN_OCCURRENCES:
        fired.add(EscalationReason.REPEAT_ISSUE)

    # 11.6 - SLA already breached OR ETA under 45 minutes.
    if signals.sla_breached or (
        signals.eta_minutes is not None
        and signals.eta_minutes < ETA_REVIEW_THRESHOLD_MIN
    ):
        fired.add(EscalationReason.SLA_OR_ETA)

    # 11.7 - guest explicitly requested a manager.
    if signals.manager_requested:
        fired.add(EscalationReason.MANAGER_REQUESTED)

    reasons = [reason for reason in _REASON_ORDER if reason in fired]
    status = EscalationStatus.MANDATORY_GM_REVIEW if reasons else EscalationStatus.NONE
    if reasons:
        logger.info(
            "Alert flagged for mandatory GM review",
            extra={"reasons": [reason.value for reason in reasons]},
        )
    return EscalationDecision(escalation_status=status, reasons=reasons)


__all__ = [
    "CONFIDENCE_REVIEW_THRESHOLD",
    "ETA_REVIEW_THRESHOLD_MIN",
    "REPEAT_ISSUE_MIN_OCCURRENCES",
    "DEFAULT_AMBASSADOR_TIERS",
    "BrandPolicy",
    "AlertSignals",
    "evaluate_escalation_triggers",
]
