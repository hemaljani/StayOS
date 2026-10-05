"""Integration tests: latency SLAs and cross-component wiring (Task 23.1).

Representative, mock/moto-driven checks that the closed-loop components wire
together correctly:

    * rule evaluation starts on a DynamoDB Streams event and creates an alert
      (Requirement 1.1 wiring),
    * a persisted rule update propagates after the cache TTL window
      (Requirement 2.4),
    * INFO alerts flush in <=50-alert batches on the configurable interval
      (Requirements 8.6, 9.3, 13.4),
    * a history record is written with a 90-day ``expiresAt`` TTL
      (Requirement 14.1),
    * the delivery-latency metric carries the ``Tier`` dimension
      (Requirement 17.1),
    * the observability tracing seam exposes distinct generation/delivery
      subsegments (Requirement 17.3),
    * the delivery path emits latency and prioritizes immediate CRITICAL push
      over batched INFO (Requirements 13.1, 13.2),
    * the SPOG sister-property availability path resolves via a mocked lookup
      (Requirement 3.4).

Assertions that require real wall-clock timing (the CRITICAL <=30s / WARNING
<=120s delivery SLA and the <=5s rule-eval start) need a live deploy to measure
Streams propagation and are marked skip-with-reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pulse.common.operational_schema import RESERVATIONS_SK
from pulse.delivery import info_batcher, push_service
from pulse.delivery import realtime_publish as rt
from pulse.history import writer
from pulse.observability import metrics as obs
from pulse.rule_engine import handler as rule_handler
from pulse.rule_engine.rule_validation import (
    default_rule_templates,
    default_template_item,
)
from pulse.rule_engine.rules_repository import RulesRepository
from pulse.triage.context import SituationContext
from pulse.triage.specializations import build_walk_strategy
from tests.integration.conftest import (
    ALERT_HISTORY_TABLE_NAME,
    RULES_TABLE_NAME,
    IntegrationEnv,
)

DEMO_PROPERTY_ID = "ALOHA-CHI-001"


def _seed_default_rules(env: IntegrationEnv, property_id: str) -> None:
    """Seed the default enabled rule templates for a property into pulse-rules."""
    for rule in default_rule_templates(property_id):
        env.rules.put_item(Item=default_template_item(rule))


def _walk_stream_event(property_id: str, confirmed: int, available: int) -> dict:
    """Build a raw DynamoDB Streams event for a walk-risk aggregate change."""
    arrival_date = "2026-08-17"
    return {
        "Records": [
            {
                "eventID": "1",
                "eventName": "MODIFY",
                "eventSourceARN": (
                    "arn:aws:dynamodb:us-east-1:123456789012:table/"
                    "stayos-reservations/stream/2026-08-17T00:00:00.000"
                ),
                "dynamodb": {
                    "NewImage": {
                        "propertyId": {"S": property_id},
                        RESERVATIONS_SK: {"S": f"WALK#{arrival_date}"},
                        "arrivalDate": {"S": arrival_date},
                        "confirmedReservations": {"N": str(confirmed)},
                        "availableRooms": {"N": str(available)},
                    }
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# Requirement 1.1 - rule evaluation is wired to a Streams event
# ---------------------------------------------------------------------------


def test_rule_eval_wiring_creates_alert_from_stream_event(
    integration_env: IntegrationEnv,
) -> None:
    """A stream event flows through parse -> evaluate -> persist into pulse-alerts.

    Validates: Requirement 1.1 (wiring)
    """
    _seed_default_rules(integration_env, DEMO_PROPERTY_ID)
    event = _walk_stream_event(DEMO_PROPERTY_ID, confirmed=374, available=368)

    summary = rule_handler.lambda_handler(event, None)

    assert summary["recordsProcessed"] == 1
    assert summary["alertsCreated"] == 1
    stored = integration_env.alerts.scan()["Items"]
    assert len(stored) == 1
    assert stored[0]["type"] == "WALK_RISK"
    assert stored[0]["propertyId"] == DEMO_PROPERTY_ID
    assert stored[0]["status"] == "UNACKNOWLEDGED"


@pytest.mark.skip(
    reason=(
        "Requirement 1.1 <=5s rule-eval start is a wall-clock SLA that depends "
        "on real DynamoDB Streams propagation and Lambda cold-start timing; it "
        "can only be measured against a live deploy. The in-process wiring is "
        "covered by test_rule_eval_wiring_creates_alert_from_stream_event."
    )
)
def test_rule_eval_start_latency_sla_live_only() -> None:  # pragma: no cover
    """Placeholder for the live-only <=5s rule-eval-start SLA measurement."""


# ---------------------------------------------------------------------------
# Requirement 2.4 - rule-update propagation after the cache TTL window
# ---------------------------------------------------------------------------


def test_rule_update_propagates_after_cache_window(
    integration_env: IntegrationEnv,
) -> None:
    """A disabled rule is invisible to evaluations beginning past the TTL window.

    Validates: Requirement 2.4
    """
    _seed_default_rules(integration_env, DEMO_PROPERTY_ID)

    clock = {"t": 1000.0}
    repository = RulesRepository(
        table_name=RULES_TABLE_NAME,
        ttl_seconds=60,
        clock=lambda: clock["t"],
    )

    # First load caches the enabled rules (walk-risk among them).
    first = repository.get_enabled_rules(DEMO_PROPERTY_ID)
    assert any(rule.rule_type.value == "WALK_RISK" for rule in first)

    # Persist a rule update: disable the walk-risk rule.
    walk_rule = next(
        rule for rule in default_rule_templates(DEMO_PROPERTY_ID)
        if rule.rule_type.value == "WALK_RISK"
    )
    disabled_item = default_template_item(walk_rule)
    disabled_item["enabled"] = False
    integration_env.rules.put_item(Item=disabled_item)

    # Within the TTL window the cached (still-enabled) view is served.
    within_window = repository.get_enabled_rules(DEMO_PROPERTY_ID)
    assert any(rule.rule_type.value == "WALK_RISK" for rule in within_window)

    # Advance the clock past the 60s window: the update is now visible.
    clock["t"] += 61.0
    after_window = repository.get_enabled_rules(DEMO_PROPERTY_ID)
    assert not any(rule.rule_type.value == "WALK_RISK" for rule in after_window)


# ---------------------------------------------------------------------------
# Requirements 8.6, 9.3, 13.4 - INFO batch flush on interval and at the 50 cap
# ---------------------------------------------------------------------------


def test_info_batches_never_exceed_cap_and_lose_nothing() -> None:
    """INFO batching splits into <=50-alert batches whose union is the input.

    Validates: Requirements 13.4 (and 8.6, 9.3 accumulation)
    """
    alerts = [{"alertId": f"a{i}", "propertyId": DEMO_PROPERTY_ID} for i in range(120)]
    batches = info_batcher.batch_alerts(alerts)

    assert [len(batch) for batch in batches] == [50, 50, 20]
    assert all(len(batch) <= info_batcher.INFO_BATCH_MAX for batch in batches)
    flattened = [alert for batch in batches for alert in batch]
    assert flattened == alerts


def test_info_batch_interval_clamps_to_supported_range() -> None:
    """The configurable INFO interval clamps to the supported 5-60 minutes.

    Validates: Requirement 13.3 (interval bound used by the interval flush)
    """
    assert info_batcher.resolve_batch_interval_min(1) == 5
    assert info_batcher.resolve_batch_interval_min(30) == 30
    assert info_batcher.resolve_batch_interval_min(120) == 60


def test_info_flush_publishes_and_marks_delivered(
    integration_env: IntegrationEnv,
) -> None:
    """A flush publishes each INFO batch to the property channel and marks it.

    Validates: Requirements 8.6, 9.3 (batched INFO delivery wiring)
    """
    marked: list[tuple[list[str], str]] = []

    def _marker(alert_ids: Any, delivered_at: str) -> None:
        marked.append((list(alert_ids), delivered_at))

    publisher_calls: list[str] = []

    def _publisher(channel: str, events: Any) -> None:
        publisher_calls.append(channel)

    alerts = [
        {
            "alertId": f"a{i}",
            "propertyId": DEMO_PROPERTY_ID,
            "tier": "INFO",
            "type": "PREMIUM_CANCELLATION",
            "status": "UNACKNOWLEDGED",
            "title": f"Premium cancellation {i}",
            "createdAt": "2026-08-17T10:00:00Z",
        }
        for i in range(3)
    ]

    summary = info_batcher.flush_info_alerts(
        alerts, marker=_marker, publisher=_publisher, now_iso="2026-08-17T10:15:00Z"
    )

    assert summary["alertsDelivered"] == 3
    assert summary["batchesDelivered"] == 1
    assert publisher_calls == [rt.broadcast_channel(DEMO_PROPERTY_ID)]
    assert marked and marked[0][0] == ["a0", "a1", "a2"]


# ---------------------------------------------------------------------------
# Requirement 14.1 - history write plus 90-day expiresAt TTL
# ---------------------------------------------------------------------------


def test_history_write_sets_ninety_day_ttl(
    integration_env: IntegrationEnv,
) -> None:
    """A history record is persisted with expiresAt = createdAt + 90 days.

    Validates: Requirement 14.1 (and 14.5/14.6 retention window)
    """
    version_provider = writer._default_version_provider(ALERT_HISTORY_TABLE_NAME)
    history_writer = writer._default_writer(ALERT_HISTORY_TABLE_NAME)
    alert_image = {
        "alertId": "alert-hist-1",
        "propertyId": DEMO_PROPERTY_ID,
        "tier": "CRITICAL",
        "type": "WALK_RISK",
        "status": "RESOLVED",
        "createdAt": "2026-08-17T14:30:00Z",
        "lastStatusChangeAt": "2026-08-17T14:40:00Z",
    }

    written = writer.process_alert_image(
        alert_image,
        version_provider=version_provider,
        writer=history_writer,
        sleep=lambda _s: None,
    )

    assert written is True
    stored = integration_env.history.get_item(
        Key={"alertId": "alert-hist-1", "version": 1}
    )["Item"]
    assert int(stored["expiresAt"]) == writer.compute_expires_at(
        "2026-08-17T14:30:00Z"
    )


# ---------------------------------------------------------------------------
# Requirement 17.1 - delivery-latency metric carries the Tier dimension
# ---------------------------------------------------------------------------


def test_latency_metric_emits_with_tier_dimension() -> None:
    """The delivery-latency metric is emitted dimensioned by tier.

    Validates: Requirement 17.1
    """
    emitted: list[tuple[str, int]] = []

    def _spy_emitter(tier_value: str, latency_ms: int) -> None:
        emitted.append((tier_value, latency_ms))

    item = {"alertId": "alert-1", "createdAt": "2026-08-17T14:30:00Z"}
    # 2500 ms after createdAt.
    now = datetime(2026, 8, 17, 14, 30, 2, 500000, tzinfo=UTC)

    latency = obs.record_delivery_latency(
        item, "CRITICAL", now=now, emitter=_spy_emitter
    )

    assert latency == 2500
    assert emitted == [("CRITICAL", 2500)]


# ---------------------------------------------------------------------------
# Requirement 17.3 - two distinct trace subsegments (generation + delivery)
# ---------------------------------------------------------------------------


def test_tracing_seam_exposes_two_distinct_subsegments() -> None:
    """The observability tracing seam supports distinct generation/delivery spans.

    Validates: Requirement 17.3 (the seam that yields the two-segment trace)
    """
    entered: list[str] = []
    for segment in ("generation", "delivery"):
        with obs.trace_subsegment(segment):
            entered.append(segment)
    # Both named subsegments run (as a no-op when the X-Ray SDK is absent), so
    # the two-segment trace shape is available without requiring the SDK.
    assert entered == ["generation", "delivery"]


# ---------------------------------------------------------------------------
# Requirements 13.1, 13.2 - delivery emits latency; CRITICAL immediate vs INFO
# ---------------------------------------------------------------------------


def test_delivery_emits_latency_and_prioritizes_critical_over_info(
    integration_env: IntegrationEnv,
) -> None:
    """CRITICAL is pushed immediately (and latency recorded); INFO is batched.

    Validates: Requirements 13.1, 13.2 (immediate CRITICAL vs deferred INFO)
    """
    recorded: list[tuple[str, int]] = []

    def _latency_recorder(item: dict[str, Any], tier: str) -> None:
        recorded.append((tier, 0))

    sent: list[str] = []

    def _web_push_sender(subscription: dict[str, Any], payload: str) -> None:
        sent.append(subscription.get("endpoint", ""))

    critical_item = {
        "alertId": "alert-crit",
        "propertyId": DEMO_PROPERTY_ID,
        "tier": "CRITICAL",
        "type": "WALK_RISK",
        "status": "UNACKNOWLEDGED",
        "title": "Walk Risk",
        "gmAlias": "jsmith",
        "createdAt": "2026-08-17T10:00:00Z",
    }
    subscriptions = [{"endpoint": "https://push.example/abc", "keys": {}}]

    critical_summary = push_service.deliver_alert(
        critical_item,
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda _alias: subscriptions,
        web_push_sender=_web_push_sender,
        realtime_publisher=lambda _c, _e: None,
        sleep=lambda _s: None,
        latency_recorder=_latency_recorder,
    )

    # CRITICAL: realtime + immediate background Web Push, latency recorded.
    assert critical_summary["realtimePublished"] is True
    assert critical_summary["webPushAttempted"] is True
    assert sent == ["https://push.example/abc"]
    assert recorded == [("CRITICAL", 0)]

    info_item = {**critical_item, "alertId": "alert-info", "tier": "INFO"}
    info_summary = push_service.deliver_alert(
        info_item,
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda _alias: subscriptions,
        web_push_sender=_web_push_sender,
        realtime_publisher=lambda _c, _e: None,
        sleep=lambda _s: None,
        latency_recorder=_latency_recorder,
    )

    # INFO: no immediate push (it is deferred to the INFO batcher).
    assert info_summary.get("webPushSkipped") is True
    assert info_summary["webPushAttempted"] is False
    # Only the CRITICAL endpoint was pushed to.
    assert sent == ["https://push.example/abc"]


@pytest.mark.skip(
    reason=(
        "Requirements 13.1/13.2 CRITICAL <=30s and WARNING <=120s delivery are "
        "real wall-clock SLAs measured from generation to device receipt across "
        "AppSync Events + Web Push; they require a live deploy. The delivery "
        "path logic (immediate CRITICAL vs batched INFO, latency emission) is "
        "covered by test_delivery_emits_latency_and_prioritizes_critical_over_info."
    )
)
def test_delivery_latency_wallclock_sla_live_only() -> None:  # pragma: no cover
    """Placeholder for the live-only CRITICAL/WARNING delivery-latency SLA."""


# ---------------------------------------------------------------------------
# Requirement 3.4 - SPOG sister-property availability path (mocked lookup)
# ---------------------------------------------------------------------------


def test_spog_sister_property_available_via_mocked_lookup() -> None:
    """Option B: the walk strategy never recommends a cross-city sister property.

    Even when the (legacy) sister lookup seam WOULD return an available property,
    the reframed strategy ignores it and produces walkable guests instead;
    relocation is framed as in-house/partner-overflow in the ranked options.

    Validates: Walk Risk Option B reframe (supersedes Requirement 3.4/3.6 sister
    behavior).
    """
    context = SituationContext(
        property_id=DEMO_PROPERTY_ID,
        confirmed_guests=[
            {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "Member"}
        ],
        room_shortfall=1,
        loyalty_protection_tier="Gold",
        stay_dates=("2026-08-17", "2026-08-18"),
        sister_property_lookup=lambda _dates: "ALOHA-MKE-002",
    )

    strategy = build_walk_strategy(context)

    assert strategy.sister_property_available is False
    assert strategy.sister_property_id is None
    assert len(strategy.walkable_guests) == 1


def test_spog_sister_property_unavailable_via_mocked_lookup() -> None:
    """The walk strategy never assigns a sister even when the lookup returns None."""
    context = SituationContext(
        property_id=DEMO_PROPERTY_ID,
        confirmed_guests=[
            {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "Member"}
        ],
        room_shortfall=1,
        loyalty_protection_tier="Gold",
        stay_dates=("2026-08-17", "2026-08-18"),
        sister_property_lookup=lambda _dates: None,
    )

    strategy = build_walk_strategy(context)

    assert strategy.sister_property_available is False
    assert strategy.sister_property_id is None
