"""Unit tests for the Triage Agent Bedrock client (mocked model).

Covers the tier latency-target enforcement (Requirement 10.6), end-to-end brief
assembly per alert type with an injected invoker, and the Rule Engine invoker
adapter. No live Bedrock call is ever made - the invoker seam is always mocked.
"""

from __future__ import annotations

import json

import pytest

from pulse.common.errors import TriageFailure
from pulse.common.models import AlertTier, AlertType
from pulse.triage.bedrock_client import generate_triage_brief, make_rule_engine_invoker
from pulse.triage.context import SituationContext
from tests.triage.conftest import make_draft, make_invoker

_BASE_JSON = json.dumps(
    {
        "summary": "Confirmed reservations exceed available rooms.",
        "confidence": 88,
        "options": [
            {
                "label": "A",
                "rank": 1,
                "title": "Walk lowest-tier guests",
                "detail": "Relocate 6.",
                "recommended": True,
            },
            {
                "label": "B",
                "rank": 2,
                "title": "Hold rooms",
                "detail": "Delay assignment.",
                "recommended": False,
            },
        ],
        "executeLabel": "Approve Walk Strategy A",
    }
)


def _walk_context() -> SituationContext:
    """Build a Walk Risk situation context with a sister property available."""
    return SituationContext(
        property_id="ALOHA-CHI-001",
        confirmed_guests=[
            {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "Gold"}
        ],
        room_shortfall=1,
        loyalty_protection_tier="Gold",
        stay_dates=("2026-08-17", "2026-08-19"),
        sister_property_lookup=lambda _dates: "ALOHA-CHI-002",
    )


def test_walk_risk_brief_generated_within_budget() -> None:
    """A CRITICAL Walk Risk brief is produced and the Walk_Strategy attached."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, clock = make_invoker(_BASE_JSON, delay=1.0)  # under the 5 s budget

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88
    assert len(brief.options) == 2
    assert brief.walk_strategy is not None
    # Option B: the walk strategy never recommends a cross-city sister property,
    # even though _walk_context supplies a lookup that would return one.
    assert brief.walk_strategy.sister_property_available is False
    assert brief.walk_strategy.sister_property_id is None
    assert len(brief.walk_strategy.walkable_guests) == 1


def test_requirement_10_6_critical_latency_breach_fails() -> None:
    """Exceeding the CRITICAL 5 s target raises a timeout TriageFailure.

    Validates: Requirement 10.6
    """
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, clock = make_invoker(_BASE_JSON, delay=6.0)  # over the 5 s budget

    with pytest.raises(TriageFailure) as excinfo:
        generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)
    assert excinfo.value.reason == "timeout"


def test_requirement_10_6_warning_latency_budget_is_higher() -> None:
    """A WARNING alert tolerates up to 15 s; 6 s succeeds where CRITICAL fails.

    Validates: Requirement 10.6
    """
    ooo_json = json.dumps(
        {"summary": "OOO cluster overlaps a group block.", "confidence": 75}
    )
    draft = make_draft(AlertType.OOO_CLUSTER, AlertTier.WARNING)
    invoker, clock = make_invoker(ooo_json, delay=6.0)  # under the 15 s WARNING budget
    context = SituationContext(
        property_id="ALOHA-CHI-001",
        required_room_type="KING",
        replacement_candidates=[
            {
                "roomId": "RM-1",
                "roomType": "KING",
                "availableForRange": True,
                "suitability": 0.9,
            },
        ],
    )

    brief = generate_triage_brief(draft, context, invoker=invoker, clock=clock)

    assert brief.confidence == 75
    # OOO options are assembled from context, not from the model.
    assert len(brief.options) == 1


def test_invalid_json_becomes_triage_failure() -> None:
    """Non-JSON model output is a TriageFailure (deliver without a brief)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, clock = make_invoker("not json at all", delay=0.5)

    with pytest.raises(TriageFailure) as excinfo:
        generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)
    assert excinfo.value.reason == "invalid_json"


def test_json_wrapped_in_markdown_fence_is_parsed() -> None:
    """A ```json-fenced brief still parses (BUG-032: tolerant extraction).

    A Strands Agent that ran tool-use turns frequently wraps its final answer in
    a markdown code fence despite the strict-JSON instruction; strict json.loads
    used to drop every such brief as invalid_json (no "Agent ready" badge).
    """
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    fenced = f"```json\n{_BASE_JSON}\n```"
    invoker, clock = make_invoker(fenced, delay=1.0)

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88
    assert len(brief.options) == 2


def test_json_with_surrounding_prose_is_parsed() -> None:
    """A brief with a lead-in sentence + trailing note still parses (BUG-032)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    noisy = f"Here is the triage brief:\n{_BASE_JSON}\nLet me know if you need more."
    invoker, clock = make_invoker(noisy, delay=1.0)

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88
    assert len(brief.options) == 2


def test_bare_fence_without_language_tag_is_parsed() -> None:
    """A plain ``` fence (no ``json`` tag) still parses (BUG-032)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    fenced = f"```\n{_BASE_JSON}\n```"
    invoker, clock = make_invoker(fenced, delay=1.0)

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88


def test_extract_json_text_recovers_object_and_rejects_prose() -> None:
    """_extract_json_text narrows to the object; genuinely-empty text stays empty."""
    from pulse.triage.bedrock_client import _extract_json_text

    assert _extract_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _extract_json_text('prose {"a": 1} tail') == '{"a": 1}'
    assert _extract_json_text('{"a": 1}') == '{"a": 1}'
    # No JSON object present -> returned trimmed as-is (json.loads then fails).
    assert _extract_json_text("no object here") == "no object here"


def _sequence_invoker(outputs: list[str]) -> tuple:
    """Build an invoker that returns each output in turn, plus a monotonic clock.

    Lets a test simulate a first-attempt flake followed by a valid second
    attempt (BUG-021 retry). The clock reports zero elapsed so the tier latency
    budget never trips.
    """
    calls = {"i": 0}

    def _invoker(_model_id: str, _prompt: str) -> str:
        idx = min(calls["i"], len(outputs) - 1)
        calls["i"] += 1
        return outputs[idx]

    def _clock() -> float:
        return 0.0

    return _invoker, _clock, calls


def test_retry_recovers_after_first_attempt_flake() -> None:
    """A retryable first-attempt failure is retried once and then succeeds (BUG-021)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    # First attempt: invalid JSON (retryable). Second attempt: a valid brief.
    invoker, clock, calls = _sequence_invoker(["not json at all", _BASE_JSON])

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88
    assert calls["i"] == 2  # exactly one retry


def test_retry_gives_up_after_second_failure() -> None:
    """Two consecutive retryable failures still surface a TriageFailure."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, clock, calls = _sequence_invoker(["garbage one", "garbage two"])

    with pytest.raises(TriageFailure) as excinfo:
        generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)
    assert excinfo.value.reason == "invalid_json"
    assert calls["i"] == 2  # tried twice, then gave up


def test_success_on_first_attempt_does_not_retry() -> None:
    """A valid first attempt is used as-is (no wasted second invocation)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, clock, calls = _sequence_invoker([_BASE_JSON, "should-not-be-used"])

    brief = generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)

    assert brief.confidence == 88
    assert calls["i"] == 1  # no retry


def test_timeout_failure_is_not_retried() -> None:
    """A latency-budget breach is non-retryable (retrying would blow it further)."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    # delay 6s > CRITICAL 5s budget on the first call -> timeout, no retry.
    invoker, clock = make_invoker(_BASE_JSON, delay=6.0)

    with pytest.raises(TriageFailure) as excinfo:
        generate_triage_brief(draft, _walk_context(), invoker=invoker, clock=clock)
    assert excinfo.value.reason == "timeout"


def test_complaint_brief_enforces_option_bounds() -> None:
    """Complaint assembly builds 3-5 cost/risk options from the model output."""
    complaint_json = json.dumps(
        {
            "summary": "Guest complaint escalated past the authority threshold.",
            "confidence": 70,
            "options": [
                {
                    "label": "A",
                    "rank": 1,
                    "title": "Comp two nights",
                    "detail": "x",
                    "estimatedCost": 480.0,
                    "reviewRisk": "Low",
                    "recommended": True,
                },
                {
                    "label": "B",
                    "rank": 2,
                    "title": "Upgrade + dining",
                    "detail": "y",
                    "estimatedCost": 220.0,
                    "reviewRisk": "Medium",
                },
                {
                    "label": "C",
                    "rank": 3,
                    "title": "Points + apology",
                    "detail": "z",
                    "estimatedCost": 90.0,
                    "reviewRisk": "High",
                },
            ],
        }
    )
    draft = make_draft(AlertType.COMPLAINT_ESCALATION, AlertTier.CRITICAL)
    invoker, clock = make_invoker(complaint_json, delay=0.5)
    context = SituationContext(property_id="ALOHA-CHI-001", currency="USD")

    brief = generate_triage_brief(draft, context, invoker=invoker, clock=clock)

    assert 3 <= len(brief.options) <= 5
    assert sum(1 for o in brief.options if o.recommended) == 1
    assert all(o.estimated_cost is not None for o in brief.options)


def test_make_rule_engine_invoker_adapts_to_seam() -> None:
    """The adapter supplies context and returns a brief for a draft."""
    draft = make_draft(AlertType.WALK_RISK, AlertTier.CRITICAL)
    invoker, _clock = make_invoker(_BASE_JSON, delay=0.0)

    rule_invoker = make_rule_engine_invoker(
        lambda _draft: _walk_context(), invoker=invoker
    )
    brief = rule_invoker(draft)

    assert brief is not None
    assert brief.walk_strategy is not None


# ---------------------------------------------------------------------------
# INFO advisory-opportunity briefs (Premium Cancellation, VIP Check-In)
# ---------------------------------------------------------------------------

_INFO_JSON = json.dumps(
    {
        "summary": "Premium room freed by a cancellation; resell before arrival.",
        "confidence": 72,
        "options": [
            {
                "label": "A",
                "rank": 1,
                "title": "Re-list at current rate",
                "detail": "Pace is healthy; re-list across OTA + direct.",
                "recommended": True,
            },
            {
                "label": "B",
                "rank": 2,
                "title": "Offer as a paid upgrade",
                "detail": "Upsell a confirmed lower-tier guest.",
                "recommended": False,
            },
        ],
        "executeLabel": "Re-list premium room",
    }
)


def test_premium_cancellation_advisory_brief_is_generated() -> None:
    """An INFO Premium Cancellation produces an advisory brief (no specialization)."""
    draft = make_draft(AlertType.PREMIUM_CANCELLATION, AlertTier.INFO)
    invoker, clock = make_invoker(_INFO_JSON, delay=1.0)
    context = SituationContext(
        property_id="ALOHA-CHI-001",
        currency="USD",
        extra={"revenue": {"adr": 300}, "occupancy": {"availableRooms": 8}},
    )

    brief = generate_triage_brief(draft, context, invoker=invoker, clock=clock)

    assert brief.confidence == 72
    assert 2 <= len(brief.options) <= 5
    # Advisory INFO briefs carry no Walk_Strategy specialization.
    assert brief.walk_strategy is None


def test_vip_checkin_advisory_brief_is_generated() -> None:
    """An INFO VIP Check-In produces an advisory welcome/upsell brief."""
    vip_json = json.dumps(
        {
            "summary": "Ambassador VIP checked in; deliver a standout welcome.",
            "confidence": 80,
            "options": [
                {
                    "label": "A",
                    "rank": 1,
                    "title": "Preferred-room welcome amenity",
                    "detail": "High floor + sparkling water per preferences.",
                    "recommended": True,
                },
                {
                    "label": "B",
                    "rank": 2,
                    "title": "Suite upgrade offer",
                    "detail": "A suite is available; offer a paid upgrade.",
                    "recommended": False,
                },
            ],
            "executeLabel": "Send VIP welcome",
        }
    )
    draft = make_draft(AlertType.VIP_CHECKIN, AlertTier.INFO)
    invoker, clock = make_invoker(vip_json, delay=1.0)
    context = SituationContext(
        property_id="ALOHA-CHI-001",
        extra={"vipGuests": [{"guestId": "G-9", "loyaltyTier": "AMBASSADOR"}]},
    )

    brief = generate_triage_brief(draft, context, invoker=invoker, clock=clock)

    assert brief.confidence == 80
    assert 2 <= len(brief.options) <= 5
    assert brief.walk_strategy is None
