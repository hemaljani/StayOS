"""End-to-end unit tests for the Triage Agent orchestration (``handle_triage``).

Drives the whole flow with every boundary faked: the Gateway tool caller, the
narrative model invoker, DynamoDB, and the realtime publisher. Covers:
    * Walk Risk and OOO Cluster produce a brief that passes validation and
      satisfies the per-type structural guarantee (rank-ordered, <= 1
      recommended -- Property 18).
    * The attach is conditional: an already-terminal alert is skipped (no
      update, no publish) -- idempotent.
    * A simulated triage failure/timeout records the failure, does not raise,
      and does not attach a brief.
    * Every Gateway tool call passes the correct propertyId.
"""

from __future__ import annotations

import json

from server import handle_triage

from tests.triage_agent.conftest import (
    FakeAlertsTable,
    RecordingPublisher,
    RecordingToolCaller,
    make_json_invoker,
    make_step_clock,
)

_PID = "ALOHA-CHI-001"

_WALK_JSON = json.dumps(
    {
        "summary": "Confirmed reservations exceed available rooms by six.",
        "confidence": 88,
        "options": [
            {
                "label": "A",
                "rank": 1,
                "title": "Walk lowest-tier guests",
                "detail": "Relocate 6 to the sister property.",
                "recommended": True,
            },
            {
                "label": "B",
                "rank": 2,
                "title": "Hold assignment",
                "detail": "Delay room assignment.",
                "recommended": False,
            },
        ],
        "executeLabel": "Approve Walk Strategy A",
    }
)

_OOO_JSON = json.dumps(
    {"summary": "OOO cluster overlaps a group block.", "confidence": 75}
)


def _walk_tools() -> RecordingToolCaller:
    """Gateway results for a Walk Risk alert with a sister property available."""
    return RecordingToolCaller(
        {
            "get_occupancy": {"arrivals": 8, "availableRooms": 2, "date": "2026-08-17"},
            "get_walkable_guests": [
                {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "PLATINUM"},
                {"guestId": "G-2", "reservationId": "R-2", "loyaltyTier": "PLATINUM"},
            ],
            "get_sister_property_availability": [
                {"propertyId": "ALOHA-CHI-002", "availableRooms": 4},
            ],
        }
    )


def _non_terminal_table() -> FakeAlertsTable:
    """A pulse-alerts fake holding a non-terminal alert item."""
    return FakeAlertsTable(
        {
            "alertId": "alert-1",
            "propertyId": _PID,
            "tier": "CRITICAL",
            "type": "WALK_RISK",
            "status": "UNACKNOWLEDGED",
            "title": "Walk Risk",
        }
    )


def _payload(alert_type: str, tier: str) -> dict[str, str]:
    """Build an InvokeAgentRuntime payload for an alert."""
    return {
        "alertId": "alert-1",
        "alertType": alert_type,
        "propertyId": _PID,
        "tier": tier,
    }


def _assert_property_18(brief_item: dict) -> None:
    """Assert a brief's options are rank-ordered with at most one recommended."""
    ranks = [option["rank"] for option in brief_item["options"]]
    assert ranks == sorted(ranks), "options must be ordered highest to lowest rank"
    assert len(set(ranks)) == len(ranks), "ranks must be unique"
    labels = [option["label"] for option in brief_item["options"]]
    assert len(set(labels)) == len(labels), "labels must be unique"
    recommended = sum(1 for option in brief_item["options"] if option["recommended"])
    assert recommended <= 1, "at most one option may be recommended"


def test_walk_risk_produces_valid_brief_and_attaches() -> None:
    """Walk Risk: brief passes validation + Property 18, attaches, publishes.

    Validates: Requirements 10.1, 10.2, 3.3, 3.4, 3.5, 3.6 (Property 18).
    """
    caller = _walk_tools()
    table = _non_terminal_table()
    publisher = RecordingPublisher()

    result = handle_triage(
        _payload("WALK_RISK", "CRITICAL"),
        tool_caller=caller,
        invoker=make_json_invoker(_WALK_JSON),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
        wall_clock=make_step_clock([0.0, 1.0]),
        now_fn=lambda: "2026-08-17T14:40:00Z",
    )

    assert "triageBrief" in result
    _assert_property_18(result["triageBrief"])
    # Walk Risk carries a Walk_Strategy with walkable guests but NO cross-city
    # sister property (Option B: relocation is in-house/partner-overflow, framed
    # in the ranked options, not a same-brand sister hotel).
    strategy = result["triageBrief"]["walkStrategy"]
    assert strategy["sisterPropertyAvailable"] is False
    assert strategy["sisterPropertyId"] is None
    assert len(strategy["walkableGuests"]) == 2
    # Attached + published.
    assert result["attached"] is True
    assert result["published"] is True
    assert len(table.updates) == 1
    assert publisher.published
    # Only occupancy + walkable-guests are queried now (no sister-availability
    # call), each scoped by the correct propertyId.
    assert caller.property_ids() == [_PID, _PID]


def test_ooo_cluster_produces_type_matched_options() -> None:
    """OOO Cluster: options are type-matched, ranked, and <= 5 (Property 18).

    Validates: Requirements 7.2, 7.4 (Property 18).
    """
    caller = RecordingToolCaller(
        {
            "get_room_status": [
                {"roomId": "101", "roomType": "KING", "status": "OOO"},
                {"roomId": "102", "roomType": "KING", "status": "OOO"},
            ],
            "get_room_move_candidates": [
                {"roomId": "201", "roomType": "KING", "suitability": 0.9},
                {"roomId": "202", "roomType": "KING", "suitability": 0.7},
            ],
        }
    )
    table = FakeAlertsTable(
        {
            "alertId": "alert-1",
            "propertyId": _PID,
            "tier": "WARNING",
            "type": "OOO_CLUSTER",
            "status": "UNACKNOWLEDGED",
            "title": "OOO Cluster",
        }
    )
    publisher = RecordingPublisher()

    result = handle_triage(
        _payload("OOO_CLUSTER", "WARNING"),
        tool_caller=caller,
        invoker=make_json_invoker(_OOO_JSON),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
        wall_clock=make_step_clock([0.0, 2.0]),
        now_fn=lambda: "2026-08-17T14:40:00Z",
    )

    assert "triageBrief" in result
    options = result["triageBrief"]["options"]
    assert 0 < len(options) <= 5
    _assert_property_18(result["triageBrief"])
    assert result["attached"] is True
    assert caller.property_ids() == [_PID, _PID]


def test_terminal_alert_is_idempotent_no_attach_no_publish() -> None:
    """An already-terminal alert is not attached to and not published.

    Validates: Requirement 10.5 (conditional attach idempotence, Decision 8).
    """
    caller = _walk_tools()
    table = FakeAlertsTable(
        {"alertId": "alert-1", "propertyId": _PID, "status": "RESOLVED"}
    )
    publisher = RecordingPublisher()

    result = handle_triage(
        _payload("WALK_RISK", "CRITICAL"),
        tool_caller=caller,
        invoker=make_json_invoker(_WALK_JSON),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
        wall_clock=make_step_clock([0.0, 1.0]),
        now_fn=lambda: "2026-08-17T14:40:00Z",
    )

    # The brief is still generated, but not attached to the terminal alert.
    assert "triageBrief" in result
    assert result["attached"] is False
    assert result["reason"] == "already-resolved"
    assert table.updates == []
    assert publisher.published == []


def test_latency_breach_records_failure_and_does_not_attach() -> None:
    """Exceeding the relaxed CRITICAL 60 s target records a triage failure.

    Validates: Requirements 1.7, 10.6 (Decision 8 relaxed budget).
    """
    caller = _walk_tools()
    table = _non_terminal_table()
    publisher = RecordingPublisher()

    result = handle_triage(
        _payload("WALK_RISK", "CRITICAL"),
        tool_caller=caller,
        invoker=make_json_invoker(_WALK_JSON),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
        # start=0, after-generate=100 -> elapsed 100 s > 60 s CRITICAL budget.
        wall_clock=make_step_clock([0.0, 100.0]),
        now_fn=lambda: "2026-08-17T14:40:00Z",
    )

    assert "triageFailure" in result
    assert result["triageFailure"]["reason"] == "timeout"
    # The already-delivered alert remains brief-less: no attach, no publish.
    assert table.updates == []
    assert publisher.published == []


def test_invalid_model_json_records_failure_and_does_not_attach() -> None:
    """Non-JSON model output is a handled triage failure (no attach, no raise).

    Validates: Requirements 1.7, 10.6.
    """
    caller = _walk_tools()
    table = _non_terminal_table()
    publisher = RecordingPublisher()

    result = handle_triage(
        _payload("WALK_RISK", "CRITICAL"),
        tool_caller=caller,
        invoker=make_json_invoker("not json at all"),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: table,
        realtime_publisher=publisher,
        wall_clock=make_step_clock([0.0, 1.0]),
        now_fn=lambda: "2026-08-17T14:40:00Z",
    )

    assert "triageFailure" in result
    assert result["triageFailure"]["reason"] == "invalid_json"
    assert table.updates == []
    assert publisher.published == []


def test_invalid_payload_records_failure() -> None:
    """A payload missing required fields is a handled failure (never raises)."""
    result = handle_triage(
        {"alertId": "alert-1"},  # missing alertType/propertyId/tier
        tool_caller=RecordingToolCaller({}),
        invoker=make_json_invoker(_WALK_JSON),
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: _non_terminal_table(),
        realtime_publisher=RecordingPublisher(),
    )

    assert "triageFailure" in result
    assert result["triageFailure"]["reason"] == "invalid_payload"
