"""Unit tests for the conditional triage-brief attach + ALERT_UPDATED publish.

Verifies the camelCase serialization of a brief, the conditional (non-terminal)
attach with its idempotence (skip when already RESOLVED, and skip on a
conditional-check race), and the best-effort ALERT_UPDATED publish.
"""

from __future__ import annotations

from attach import attach_and_publish, brief_to_item

from pulse.common.models import (
    RankedOption,
    ReviewRisk,
    TriageBrief,
    WalkStrategy,
)
from tests.triage_agent.conftest import FakeAlertsTable, RecordingPublisher

_NOW = "2026-08-17T14:40:00Z"


def _walk_brief() -> TriageBrief:
    """Build a Walk Risk brief with a Walk_Strategy for serialization tests."""
    return TriageBrief(
        summary="Oversold by 6 rooms.",
        confidence=88,
        options=[
            RankedOption(label="A", rank=1, title="Walk", detail="x", recommended=True),
            RankedOption(label="B", rank=2, title="Hold", detail="y"),
        ],
        walk_strategy=WalkStrategy(
            sister_property_id="ALOHA-CHI-002",
            sister_property_available=True,
            walkable_guests=[
                {"guest_id": "G-1", "loyalty_tier": "Gold", "reservation_id": "R-1"}
            ],
            compensation=[
                {"guest_id": "G-1", "description": "Walk pkg", "estimated_cost": 150.0}
            ],
        ),
        execute_label="Approve Walk Strategy A",
    )


def test_brief_to_item_uses_camelcase_shape() -> None:
    """The persisted brief uses the camelCase Data Models shape."""
    item = brief_to_item(_walk_brief())

    assert item["summary"] == "Oversold by 6 rooms."
    assert item["confidence"] == 88
    assert item["options"][0]["recommended"] is True
    assert item["walkStrategy"]["sisterPropertyId"] == "ALOHA-CHI-002"
    assert item["walkStrategy"]["walkableGuests"][0]["guestId"] == "G-1"
    assert item["walkStrategy"]["compensation"][0]["estimatedCost"] == 150.0
    assert item["executeLabel"] == "Approve Walk Strategy A"


def test_complaint_option_serialization_includes_cost_and_risk() -> None:
    """Complaint options serialize estimatedCost and reviewRisk in camelCase."""
    brief = TriageBrief(
        summary="Complaint escalated.",
        confidence=70,
        options=[
            RankedOption(
                label="A",
                rank=1,
                title="Comp",
                detail="x",
                recommended=True,
                estimated_cost=480.0,
                review_risk=ReviewRisk.LOW,
            ),
        ],
    )

    option = brief_to_item(brief)["options"][0]

    assert option["estimatedCost"] == 480.0
    assert option["reviewRisk"] == "Low"


def test_attach_writes_and_publishes_for_non_terminal_alert() -> None:
    """A non-terminal alert gets the brief attached and ALERT_UPDATED published."""
    table = FakeAlertsTable(
        {
            "alertId": "alert-1",
            "propertyId": "ALOHA-CHI-001",
            "tier": "CRITICAL",
            "type": "WALK_RISK",
            "status": "UNACKNOWLEDGED",
            "title": "Walk Risk",
        }
    )
    publisher = RecordingPublisher()

    result = attach_and_publish(
        "alert-1",
        _walk_brief(),
        alerts_table_name="pulse-alerts",
        now=_NOW,
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
    )

    assert result.attached is True
    assert result.published is True
    assert len(table.updates) == 1
    # The published event carries hasTriageBrief=true on the property channel.
    assert publisher.published, "expected a realtime publish"
    channel, events = publisher.published[0]
    assert channel == "/pulse/alerts/ALOHA-CHI-001"
    assert events[0]["hasTriageBrief"] is True
    assert events[0]["eventType"] == "ALERT_UPDATED"


def test_attach_converts_floats_to_decimal_for_dynamodb() -> None:
    """The written brief has NO Python float (DynamoDB rejects floats).

    Regression for the Complaint Escalation attach crash: the model brief
    carries a float estimatedCost, and the boto3 DynamoDB resource raises
    ``TypeError: Float types are not supported`` on a native float, so the whole
    invocation failed and the brief never attached (only Complaint, the type
    with a cost). attach_and_publish must convert every nested float to Decimal
    before UpdateItem.
    """
    from decimal import Decimal

    table = FakeAlertsTable(
        {
            "alertId": "alert-1",
            "propertyId": "ALOHA-CHI-001",
            "status": "UNACKNOWLEDGED",
            "title": "Complaint",
        }
    )
    complaint_brief = TriageBrief(
        summary="Complaint escalated past the authority threshold.",
        confidence=70,
        options=[
            RankedOption(
                label="A", rank=1, title="Comp", detail="x", recommended=True,
                estimated_cost=480.0, review_risk=ReviewRisk.LOW,
            ),
            RankedOption(
                label="B", rank=2, title="Upgrade", detail="y",
                estimated_cost=120.5, review_risk=ReviewRisk.MEDIUM,
            ),
        ],
    )

    result = attach_and_publish(
        "alert-1",
        complaint_brief,
        alerts_table_name="pulse-alerts",
        now=_NOW,
        table_getter=lambda _name: table,
        realtime_publisher=RecordingPublisher(),
    )

    assert result.attached is True
    written = table.updates[0]["ExpressionAttributeValues"][":brief"]

    def _assert_no_float(value: object) -> None:
        if isinstance(value, float):
            raise AssertionError(f"native float leaked to DynamoDB write: {value!r}")
        if isinstance(value, dict):
            for item in value.values():
                _assert_no_float(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _assert_no_float(item)

    _assert_no_float(written)
    # The cost is preserved as a Decimal with the human-readable value.
    assert written["options"][0]["estimatedCost"] == Decimal("480.0")
    assert written["options"][1]["estimatedCost"] == Decimal("120.5")


def test_attach_is_idempotent_when_alert_already_resolved() -> None:
    """A terminal (RESOLVED) alert is skipped: no update, no publish."""
    table = FakeAlertsTable(
        {"alertId": "alert-1", "propertyId": "P", "status": "RESOLVED"}
    )
    publisher = RecordingPublisher()

    result = attach_and_publish(
        "alert-1",
        _walk_brief(),
        alerts_table_name="pulse-alerts",
        now=_NOW,
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
    )

    assert result.attached is False
    assert result.reason == "already-resolved"
    assert table.updates == []
    assert publisher.published == []


def test_attach_skips_on_conditional_check_race() -> None:
    """A resolve racing the attach (conditional check fails) skips gracefully."""
    table = FakeAlertsTable(
        {"alertId": "alert-1", "propertyId": "P", "status": "UNACKNOWLEDGED"},
        raise_conditional=True,
    )
    publisher = RecordingPublisher()

    result = attach_and_publish(
        "alert-1",
        _walk_brief(),
        alerts_table_name="pulse-alerts",
        now=_NOW,
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
    )

    assert result.attached is False
    assert result.reason == "already-resolved"
    assert publisher.published == []


def test_attach_skips_when_alert_not_found() -> None:
    """A missing alert (deleted/unknown) is skipped without error."""
    table = FakeAlertsTable(None)
    publisher = RecordingPublisher()

    result = attach_and_publish(
        "alert-missing",
        _walk_brief(),
        alerts_table_name="pulse-alerts",
        now=_NOW,
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
    )

    assert result.attached is False
    assert result.reason == "not-found"
    assert table.updates == []
    assert publisher.published == []
