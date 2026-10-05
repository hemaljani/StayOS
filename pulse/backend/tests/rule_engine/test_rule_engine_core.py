"""Property and unit tests for the Rule Engine core.

Covers the design correctness properties for triage routing (Property 1),
structural invariants of created alerts (Property 2), disabled-rule exclusion
(Property 3), and dedupe idempotence (Property 16); plus unit tests for the
missing-source-data batch-continuation behavior (Requirement 1.6) and the
async triage-routing behavior (Requirements 1.4, 1.7). The async
InvokeAgentRuntime seam itself is covered in ``test_triage_invoker.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from pulse.common.errors import RuleEvaluationError
from pulse.common.models import AlertTier, AlertType
from pulse.rule_engine.alert_factory import build_alert_draft, derive_alert_id
from pulse.rule_engine.handler import (
    evaluate_rules,
    persist_alert_draft,
    route_for_triage,
    should_request_triage,
)
from pulse.rule_engine.rules_repository import RulesRepository
from tests.rule_engine.conftest import make_change, make_rule

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# For property tests that share a function-scoped fixture (a moto table) across
# generated examples on purpose, suppress the fixture-reset health check and
# drop the deadline (moto operations are variable-latency).
FIXTURE_PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

VALID_TIERS = [AlertTier.CRITICAL, AlertTier.WARNING, AlertTier.INFO]

# Shared declarative trigger conditions used across the core tests.
_WALK_TRIGGER = {
    "operator": "gt",
    "left": "reservations.confirmed",
    "right": "rooms.available",
}
_PREMIUM_TRIGGER = {"operator": "eq", "left": "reservation.isPremium", "right": True}


# ---------------------------------------------------------------------------
# Property 1: triage routing follows the agentTriageEnabled flag for every tier
# ---------------------------------------------------------------------------


class _SpyInvoker:
    """A triage invoker spy that records whether it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, draft: Any) -> None:
        self.calls += 1
        return None


# Feature: initial-pulse-project, Property 1: an alert is routed to the Triage
# Agent iff its rule has triage enabled -- for EVERY tier, including INFO (which
# now receives an advisory opportunity brief rather than being skipped).
@PROPERTY_SETTINGS
@given(tier=st.sampled_from(VALID_TIERS), triage_enabled=st.booleans())
def test_property_1_triage_follows_enabled_flag(
    tier: AlertTier, triage_enabled: bool
) -> None:
    """Triage is requested iff the rule enables it, for all tiers incl. INFO.

    INFO alerts (premium cancellation, VIP check-in) now receive an advisory
    brief; the routing decision is gated purely by ``agentTriageEnabled``.

    Validates: Requirements 1.4, 1.5 (as extended for INFO advisory briefs)
    """
    # Pure routing decision: enabled-flag drives it for every tier now.
    decision = should_request_triage(tier, triage_enabled)
    assert decision is triage_enabled

    # End-to-end: the invoker is called iff triage was requested, for any tier.
    spy = _SpyInvoker()
    # Use a type whose default tier matches the sampled tier so the rule is
    # coherent (VIP_CHECKIN is the INFO example).
    alert_type = (
        AlertType.VIP_CHECKIN if tier is AlertTier.INFO else AlertType.WALK_RISK
    )
    rule = make_rule(alert_type, tier, agent_triage_enabled=triage_enabled)
    draft = build_alert_draft(
        property_id="ALOHA-CHI-001",
        tier=tier,
        alert_type=rule.rule_type,
        title="t",
        detail="d",
        dedupe_key=f"{rule.rule_type.value}#ALOHA-CHI-001#k",
        source_entity_ref={"table": "lumi", "ruleType": rule.rule_type.value},
    )
    route_for_triage(draft, rule, invoker=spy)
    assert spy.calls == (1 if triage_enabled else 0)


# ---------------------------------------------------------------------------
# Property 2: Created alerts satisfy their structural invariants
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 2: Created alerts satisfy their
# structural invariants
@PROPERTY_SETTINGS
@given(
    property_id=st.text(min_size=1, max_size=40),
    tier=st.sampled_from(VALID_TIERS),
    alert_type=st.sampled_from(list(AlertType)),
    title=st.text(min_size=1, max_size=600),
    detail=st.text(min_size=1, max_size=4000),
    dedupe_key=st.text(min_size=1, max_size=120),
)
def test_property_2_structural_invariants(
    property_id: str,
    tier: AlertTier,
    alert_type: AlertType,
    title: str,
    detail: str,
    dedupe_key: str,
) -> None:
    """Every created alert satisfies the Requirement 1.2/1.3 invariants.

    Validates: Requirements 1.2, 1.3
    """
    draft = build_alert_draft(
        property_id=property_id,
        tier=tier,
        alert_type=alert_type,
        title=title,
        detail=detail,
        dedupe_key=dedupe_key,
        source_entity_ref={"table": "lumi", "ruleType": alert_type.value},
    )

    assert draft.alert_id.startswith("alert-")
    # alertId is deterministic in the dedupe key -> distinct conditions yield
    # distinct ids (uniqueness), identical conditions collide (dedupe).
    assert draft.alert_id == derive_alert_id(dedupe_key)
    assert draft.property_id == property_id
    assert draft.tier in {AlertTier.CRITICAL, AlertTier.WARNING, AlertTier.INFO}
    assert 1 <= len(draft.title) <= 200
    assert 1 <= len(draft.detail) <= 2000
    # createdAt is valid ISO 8601 (parseable after normalizing the Z suffix).
    parsed = datetime.fromisoformat(draft.created_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


@given(
    key_a=st.text(min_size=1, max_size=60),
    key_b=st.text(min_size=1, max_size=60),
)
@PROPERTY_SETTINGS
def test_property_2_alert_id_uniqueness_and_dedupe(key_a: str, key_b: str) -> None:
    """Distinct dedupe keys yield distinct ids; identical keys collide.

    Validates: Requirements 1.2, 1.3
    """
    assert (derive_alert_id(key_a) == derive_alert_id(key_b)) == (key_a == key_b)


# ---------------------------------------------------------------------------
# Property 3: Disabled rules never generate alerts
# ---------------------------------------------------------------------------


class _FakeRulesTable:
    """A stand-in DynamoDB table whose query returns preset rule items."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        """Return the preset items regardless of the key condition."""
        return {"Items": self.items}


def _rule_item(rule_type: str, tier: str, enabled: bool) -> dict[str, Any]:
    """Build a raw ``pulse-rules`` item for the fake table."""
    return {
        "propertyId": "ALOHA-CHI-001",
        "ruleType": rule_type,
        "tier": tier,
        "triggerCondition": {"operator": "eq", "left": 1, "right": 1},
        "parameters": {},
        "agentTriageEnabled": tier != "INFO",
        "escalationTimeoutSec": 300,
        "enabled": enabled,
    }


_TIER_BY_TYPE = {
    "WALK_RISK": "CRITICAL",
    "VIP_ROOM_NOT_READY": "CRITICAL",
    "COMPLAINT_ESCALATION": "CRITICAL",
    "OOO_CLUSTER": "WARNING",
    "PREMIUM_CANCELLATION": "INFO",
    "VIP_CHECKIN": "INFO",
}


# Feature: initial-pulse-project, Property 3: Disabled rules never generate
# alerts
@PROPERTY_SETTINGS
@given(
    flags=st.lists(
        st.tuples(st.sampled_from(list(_TIER_BY_TYPE)), st.booleans()),
        min_size=0,
        max_size=6,
        unique_by=lambda pair: pair[0],
    )
)
def test_property_3_disabled_rules_excluded(flags: list[tuple[str, bool]]) -> None:
    """The repository never returns a rule whose enabled flag is false.

    Validates: Requirements 2.3
    """
    fake = _FakeRulesTable()
    fake.items = [
        _rule_item(rule_type, _TIER_BY_TYPE[rule_type], enabled)
        for rule_type, enabled in flags
    ]

    repo = RulesRepository(
        table_name="pulse-rules",
        clock=lambda: 0.0,
        table_getter=lambda _name: fake,
    )
    loaded = repo.get_enabled_rules("ALOHA-CHI-001")

    assert all(rule.enabled for rule in loaded)
    expected_enabled = {rt for rt, enabled in flags if enabled}
    assert {rule.rule_type.value for rule in loaded} == expected_enabled


# ---------------------------------------------------------------------------
# Property 16: Duplicate source events create at most one alert
# ---------------------------------------------------------------------------


def _create_alerts_table() -> Any:
    """Create and return a moto-backed ``pulse-alerts`` table resource."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="pulse-alerts",
        KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "alertId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("pulse-alerts")


@pytest.fixture
def alerts_table() -> Any:
    """Yield a moto-backed ``pulse-alerts`` table shared across examples."""
    with mock_aws():
        yield _create_alerts_table()


# Feature: initial-pulse-project, Property 16: Duplicate source events create at
# most one alert (idempotence)
@FIXTURE_PROPERTY_SETTINGS
@given(
    property_id=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=90),
        min_size=1,
        max_size=20,
    ),
    reservation_id=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=90),
        min_size=1,
        max_size=20,
    ),
    duplicates=st.integers(min_value=2, max_value=6),
)
def test_property_16_dedupe_at_most_one(
    alerts_table: Any, property_id: str, reservation_id: str, duplicates: int
) -> None:
    """Repeated identical events persist at most one alert.

    Validates: Requirements 8.4, 9.4
    """
    dedupe_key = (
        f"PREMIUM_CANCELLATION#{property_id}#{reservation_id}#{uuid.uuid4().hex}"
    )
    draft = build_alert_draft(
        property_id=property_id,
        tier=AlertTier.INFO,
        alert_type=AlertType.PREMIUM_CANCELLATION,
        title="Premium Cancellation",
        detail="duplicate test",
        dedupe_key=dedupe_key,
        source_entity_ref={
            "table": "stayos-reservations",
            "ruleType": "PREMIUM_CANCELLATION",
        },
    )

    results = [persist_alert_draft(draft, alerts_table) for _ in range(duplicates)]

    # The first write creates the alert; every duplicate is suppressed.
    assert results[0] is True
    assert all(created is False for created in results[1:])
    item = alerts_table.get_item(Key={"alertId": draft.alert_id}).get("Item")
    assert item is not None
    # Exactly one item exists for this deterministic alertId.
    scanned = alerts_table.scan()["Items"]
    assert sum(1 for it in scanned if it["alertId"] == draft.alert_id) == 1


# ---------------------------------------------------------------------------
# Unit test: Requirement 1.6 - missing source data, continue the batch
# ---------------------------------------------------------------------------


def test_requirement_1_6_missing_source_continues_batch() -> None:
    """A rule that cannot evaluate is skipped; other rules still fire."""
    # A walk-risk rule whose required counts are absent -> raises internally and
    # is skipped; a premium-cancellation rule with valid data still fires.
    walk_rule = make_rule(AlertType.WALK_RISK, AlertTier.CRITICAL, _WALK_TRIGGER)
    premium_rule = make_rule(
        AlertType.PREMIUM_CANCELLATION,
        AlertTier.INFO,
        _PREMIUM_TRIGGER,
        agent_triage_enabled=False,
    )
    # Image satisfies premium cancellation but lacks the walk-risk counts.
    change = make_change(
        "stayos-reservations",
        {
            "propertyId": "ALOHA-CHI-001",
            "reservationId": "R-1",
            "reservationStatus": "Cancelled",
            "isPremium": True,
        },
    )

    drafts = evaluate_rules(change, [walk_rule, premium_rule])

    # The walk-risk rule was skipped (missing data); the premium alert survived.
    assert len(drafts) == 1
    assert drafts[0].type is AlertType.PREMIUM_CANCELLATION


def test_requirement_1_6_evaluate_condition_raises_on_missing_operand() -> None:
    """A required-count-missing walk-risk evaluation raises RuleEvaluationError."""
    from pulse.rule_engine.evaluators import evaluate_walk_risk

    rule = make_rule(AlertType.WALK_RISK, AlertTier.CRITICAL, _WALK_TRIGGER)
    change = make_change(
        "stayos-reservations",
        {"propertyId": "P", "arrivalDate": "2026-08-17"},
    )
    with pytest.raises(RuleEvaluationError):
        evaluate_walk_risk(change, rule)


# ---------------------------------------------------------------------------
# Unit test: Requirement 1.4/1.7 - triage is fired async, once, after delivery
# ---------------------------------------------------------------------------


def test_requirement_1_4_route_for_triage_fires_async_once() -> None:
    """route_for_triage fires exactly one async invocation for an eligible alert.

    The alert is already persisted and delivered before triage is fired, and the
    evaluator no longer attaches a brief (the runtime does). route_for_triage is
    the single seam that calls the async invoker; it must call it exactly once
    for a CRITICAL/WARNING triage-enabled alert (Requirement 1.4). Best-effort
    swallowing of invoke failures is covered in test_triage_invoker.py
    (Requirement 1.7).
    """
    calls: list[Any] = []

    def _recording_invoker(draft: Any) -> None:
        calls.append(draft)

    rule = make_rule(AlertType.WALK_RISK, AlertTier.CRITICAL, agent_triage_enabled=True)
    draft = build_alert_draft(
        property_id="ALOHA-CHI-001",
        tier=AlertTier.CRITICAL,
        alert_type=AlertType.WALK_RISK,
        title="Walk Risk",
        detail="detail",
        dedupe_key="WALK_RISK#ALOHA-CHI-001#2026-08-17",
        source_entity_ref={"table": "stayos-reservations", "ruleType": "WALK_RISK"},
    )

    route_for_triage(draft, rule, invoker=_recording_invoker)
    assert calls == [draft]
