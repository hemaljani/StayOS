"""Property and unit tests for alert history and shift-handover.

Covers Property 24 (the shift-handover listing is exactly the in-window set,
ordered by creation time descending), the Requirement 14.2 history-write retry
(3 attempts, then the record is preserved), and the Requirement 14.5/14.6 TTL
(``expiresAt = createdAt + 90 days``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.history import handover, writer

PROPERTY_SETTINGS = settings(max_examples=100)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# Property 24: shift-handover listing is the in-window set ordered desc
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 24: Shift-handover listing is the
# in-window set ordered by creation time descending
@PROPERTY_SETTINGS
@given(
    offsets=st.lists(
        st.integers(min_value=-600, max_value=600), min_size=0, max_size=40
    ),
    window_start=st.integers(min_value=-300, max_value=0),
    window_end=st.integers(min_value=1, max_value=300),
)
def test_property_24_in_window_ordered_desc(
    offsets: list[int], window_start: int, window_end: int
) -> None:
    """The listing is exactly the in-window alerts, newest first.

    Validates: Requirements 14.3
    """
    base = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    start_iso = _iso(base + timedelta(minutes=window_start))
    end_iso = _iso(base + timedelta(minutes=window_end))
    alerts = [
        {"alertId": f"a{i}", "createdAt": _iso(base + timedelta(minutes=off))}
        for i, off in enumerate(offsets)
    ]

    selected = handover.select_in_window(alerts, start_iso, end_iso)

    # Every returned alert is within the inclusive window.
    assert all(start_iso <= a["createdAt"] <= end_iso for a in selected)
    # No in-window alert is missing.
    expected_ids = {
        a["alertId"] for a in alerts if start_iso <= a["createdAt"] <= end_iso
    }
    assert {a["alertId"] for a in selected} == expected_ids
    # Ordered by createdAt descending.
    created = [a["createdAt"] for a in selected]
    assert created == sorted(created, reverse=True)


# ---------------------------------------------------------------------------
# TTL: expiresAt = createdAt + 90 days
# ---------------------------------------------------------------------------


def test_compute_expires_at_is_created_plus_ninety_days() -> None:
    created = "2026-08-17T14:30:00Z"
    expires = writer.compute_expires_at(created)
    expected = int(
        (datetime(2026, 8, 17, 14, 30, tzinfo=UTC) + timedelta(days=90)).timestamp()
    )
    assert expires == expected


def test_build_history_item_copies_attributes_and_sets_ttl() -> None:
    alert = {
        "alertId": "alert-1",
        "propertyId": "ALOHA-CHI-001",
        "tier": "CRITICAL",
        "type": "WALK_RISK",
        "status": "RESOLVED",
        "resolvedBy": "jsmith",
        "createdAt": "2026-08-17T14:30:00Z",
        "lastStatusChangeAt": "2026-08-17T14:40:00Z",
    }
    item = writer.build_history_item(alert, version=3)
    assert item["alertId"] == "alert-1"
    assert item["version"] == 3
    assert item["status"] == "RESOLVED"
    assert item["statusChangeAt"] == "2026-08-17T14:40:00Z"
    assert item["expiresAt"] == writer.compute_expires_at("2026-08-17T14:30:00Z")


# ---------------------------------------------------------------------------
# Requirement 14.2: history-write retry (3x) then preserve the record
# ---------------------------------------------------------------------------


def test_requirement_14_2_retries_then_reports_failure() -> None:
    """A failing history write retries 3 times, then returns False (preserved).

    Validates: Requirement 14.2
    """
    attempts: list[int] = []
    sleeps: list[float] = []

    def _always_fail(item: dict[str, Any]) -> None:
        attempts.append(item["version"])
        raise RuntimeError("throughput exceeded")

    item = {"alertId": "alert-1", "version": 1}
    written = writer.write_history_with_retry(
        item, _always_fail, sleep=sleeps.append
    )

    assert written is False
    assert len(attempts) == writer.HISTORY_WRITE_MAX_ATTEMPTS
    assert len(sleeps) == writer.HISTORY_WRITE_MAX_ATTEMPTS - 1


def test_requirement_14_2_succeeds_after_retry() -> None:
    """A write that succeeds on the second attempt reports success.

    Validates: Requirement 14.2
    """
    calls = {"n": 0}

    def _fail_once(item: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    written = writer.write_history_with_retry(
        {"alertId": "alert-1", "version": 1}, _fail_once, sleep=lambda _s: None
    )
    assert written is True
    assert calls["n"] == 2


def test_process_alert_image_uses_version_provider() -> None:
    stored: list[dict[str, Any]] = []
    written = writer.process_alert_image(
        {"alertId": "alert-1", "createdAt": "2026-08-17T14:30:00Z", "status": "ACK"},
        version_provider=lambda alert_id: 7,
        writer=stored.append,
        sleep=lambda _s: None,
    )
    assert written is True
    assert stored[0]["version"] == 7
