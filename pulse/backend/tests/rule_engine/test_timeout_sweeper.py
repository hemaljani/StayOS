"""Unit tests for the time-based alert auto-resolve sweeper.

Exercises ``pulse.rule_engine.timeout_sweeper.sweep_expired_alerts`` against a
moto ``pulse-alerts`` table:

    * still-open alerts older than the timeout window are resolved
      (``resolvedBy = system-timeout``) and an ``ALERT_RESOLVED`` event is
      published;
    * alerts newer than the window are left untouched;
    * already-terminal (RESOLVED) alerts are a no-op (idempotent, monotonic);
    * a realtime publish failure does not fail the sweep (best-effort).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from moto import mock_aws

from pulse.rule_engine.timeout_sweeper import sweep_expired_alerts
from tests.api.conftest import ALERTS_TABLE_NAME, create_alerts_table, make_alert_item

_NOW = "2026-08-20T18:00:00Z"


class _RecordingPublisher:
    """Records realtime publish calls (channel, events)."""

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[tuple[str, list[Mapping[str, Any]]]] = []

    def __call__(self, channel: str, events: Sequence[Mapping[str, Any]]) -> None:
        """Record a publish call."""
        self.calls.append((channel, list(events)))


def test_resolves_alerts_older_than_the_window() -> None:
    """An open alert created before the cutoff is resolved as a timeout."""
    publisher = _RecordingPublisher()
    with mock_aws():
        table = create_alerts_table()
        # Created 40 minutes before _NOW -> older than the 30-min window.
        table.put_item(
            Item=make_alert_item(
                "alert-old", "ALOHA-CHI-001", created_at="2026-08-20T17:20:00Z"
            )
        )

        resolved = sweep_expired_alerts(
            ALERTS_TABLE_NAME,
            table_getter=lambda _name: table,
            realtime_publisher=publisher,
            now=_NOW,
            timeout_minutes=30,
        )

        assert resolved == ["alert-old"]
        item = table.get_item(Key={"alertId": "alert-old"})["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolvedBy"] == "system-timeout"
        assert item["resolvedAt"] == _NOW
        # An ALERT_RESOLVED event was published for the resolved alert.
        assert len(publisher.calls) == 1


def test_leaves_recent_alerts_untouched() -> None:
    """An open alert created within the window is not resolved."""
    publisher = _RecordingPublisher()
    with mock_aws():
        table = create_alerts_table()
        # Created 10 minutes before _NOW -> within the 30-min window.
        table.put_item(
            Item=make_alert_item(
                "alert-fresh", "ALOHA-CHI-001", created_at="2026-08-20T17:50:00Z"
            )
        )

        resolved = sweep_expired_alerts(
            ALERTS_TABLE_NAME,
            table_getter=lambda _name: table,
            realtime_publisher=publisher,
            now=_NOW,
            timeout_minutes=30,
        )

        assert resolved == []
        item = table.get_item(Key={"alertId": "alert-fresh"})["Item"]
        assert item["status"] == "UNACKNOWLEDGED"
        assert publisher.calls == []


def test_already_resolved_alert_is_a_noop() -> None:
    """A terminal (RESOLVED) alert older than the window is never touched."""
    publisher = _RecordingPublisher()
    with mock_aws():
        table = create_alerts_table()
        table.put_item(
            Item=make_alert_item(
                "alert-done",
                "ALOHA-CHI-001",
                status="RESOLVED",
                created_at="2026-08-20T17:00:00Z",
            )
        )

        resolved = sweep_expired_alerts(
            ALERTS_TABLE_NAME,
            table_getter=lambda _name: table,
            realtime_publisher=publisher,
            now=_NOW,
            timeout_minutes=30,
        )

        assert resolved == []
        assert publisher.calls == []


def test_publish_failure_does_not_fail_the_sweep() -> None:
    """A realtime publish error is swallowed; the alert still resolves."""

    def _raising_publisher(_channel: str, _events: Sequence[Mapping[str, Any]]) -> None:
        raise RuntimeError("appsync down")

    with mock_aws():
        table = create_alerts_table()
        table.put_item(
            Item=make_alert_item(
                "alert-old", "ALOHA-CHI-001", created_at="2026-08-20T17:20:00Z"
            )
        )

        resolved = sweep_expired_alerts(
            ALERTS_TABLE_NAME,
            table_getter=lambda _name: table,
            realtime_publisher=_raising_publisher,
            now=_NOW,
            timeout_minutes=30,
        )

        # The sweep completed and the alert is resolved despite the publish error.
        assert resolved == ["alert-old"]
        item = table.get_item(Key={"alertId": "alert-old"})["Item"]
        assert item["status"] == "RESOLVED"
