"""Property and unit tests for the INFO alert batcher.

Covers Property 21 (every delivered INFO batch has at most 50 alerts, a flush
occurs whenever accumulation reaches 50, and the union of all delivered batches
equals the accumulated set with no alert dropped or duplicated) plus the
interval-clamp and per-property grouping behavior.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.delivery import info_batcher as ib

PROPERTY_SETTINGS = settings(max_examples=100)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def __call__(self, channel: str, events: Any) -> None:
        self.batches.append(list(events))


# ---------------------------------------------------------------------------
# Property 21: INFO batches never exceed 50 and lose no alert
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 21: INFO batches never exceed 50 and
# lose no alert
@PROPERTY_SETTINGS
@given(count=st.integers(min_value=0, max_value=260))
def test_property_21_batches_bounded_and_lossless(count: int) -> None:
    """Every batch <= 50, flush at 50, and union == accumulated set exactly.

    Validates: Requirements 13.3, 13.4
    """
    alerts = [{"alertId": f"a{i}", "propertyId": "P1"} for i in range(count)]
    batches = ib.batch_alerts(alerts)

    # Every delivered batch is within the 50-alert cap.
    assert all(len(batch) <= ib.INFO_BATCH_MAX for batch in batches)
    # A flush happens whenever accumulation reaches the cap: all but the last
    # batch are exactly full.
    for batch in batches[:-1]:
        assert len(batch) == ib.INFO_BATCH_MAX
    # The union of batches equals the accumulated set, no drop or duplicate.
    flattened = [alert for batch in batches for alert in batch]
    assert flattened == alerts
    assert {a["alertId"] for a in flattened} == {a["alertId"] for a in alerts}


def test_flush_at_exactly_fifty_produces_single_full_batch() -> None:
    alerts = [{"alertId": f"a{i}", "propertyId": "P1"} for i in range(50)]
    batches = ib.batch_alerts(alerts)
    assert len(batches) == 1
    assert len(batches[0]) == 50


def test_flush_of_fifty_one_splits_into_fifty_plus_one() -> None:
    alerts = [{"alertId": f"a{i}", "propertyId": "P1"} for i in range(51)]
    batches = ib.batch_alerts(alerts)
    assert [len(batch) for batch in batches] == [50, 1]


# ---------------------------------------------------------------------------
# Interval clamp (Requirement 13.3)
# ---------------------------------------------------------------------------


@PROPERTY_SETTINGS
@given(requested=st.integers(min_value=-100, max_value=1000))
def test_interval_clamped_to_five_to_sixty(requested: int) -> None:
    resolved = ib.resolve_batch_interval_min(requested)
    assert ib.INFO_BATCH_INTERVAL_MIN_BOUND <= resolved
    assert resolved <= ib.INFO_BATCH_INTERVAL_MAX_BOUND


def test_interval_defaults_when_unset() -> None:
    from pulse.common.config import DEFAULT_INFO_BATCH_INTERVAL_MIN

    assert ib.resolve_batch_interval_min(None) == DEFAULT_INFO_BATCH_INTERVAL_MIN


# ---------------------------------------------------------------------------
# Orchestration: grouping + flush marks and publishes every alert
# ---------------------------------------------------------------------------


def test_flush_groups_by_property_and_marks_all() -> None:
    publisher = _RecordingPublisher()
    marked: list[str] = []

    def _marker(alert_ids: Any, delivered_at: str) -> None:
        marked.extend(alert_ids)

    alerts = [
        {"alertId": "a1", "propertyId": "P1", "tier": "INFO"},
        {"alertId": "a2", "propertyId": "P2", "tier": "INFO"},
        {"alertId": "a3", "propertyId": "P1", "tier": "INFO"},
    ]
    summary = ib.flush_info_alerts(
        alerts, marker=_marker, publisher=publisher, now_iso="2026-08-17T15:00:00Z"
    )

    assert summary["propertiesFlushed"] == 2
    assert summary["alertsDelivered"] == 3
    # Every accumulated alert is marked delivered exactly once.
    assert sorted(marked) == ["a1", "a2", "a3"]
    # Realtime events were published (one batch per property here).
    published_ids = {
        event["alertId"] for batch in publisher.batches for event in batch
    }
    assert published_ids == {"a1", "a2", "a3"}
