"""Unit tests for property-scoped kitchen snapshot reads (Requirement 16.6).

Confirms :func:`pulse.api.kitchen_repository.get_kitchen` returns the snapshot
only for a property the caller is associated with, and returns ``None`` (never
leaking existence) for an out-of-scope or missing property (Property 25),
mirroring the defensive contract of ``alerts_repository.get_alert``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from moto import mock_aws

from pulse.api import kitchen_repository as repo
from tests.api.conftest import (
    KITCHEN_TABLE_NAME,
    create_kitchen_table,
    identity,
    make_kitchen_item,
    table_getter,
)


def test_get_kitchen_returns_snapshot_for_associated_property() -> None:
    """A GM reads the snapshot for a property they are associated with.

    Validates: Requirements 16.6
    """
    with mock_aws():
        table = create_kitchen_table()
        table.put_item(Item=make_kitchen_item("ALOHA-CHI-001"))
        caller = identity("jsmith", {"ALOHA-CHI-001"})

        snapshot = repo.get_kitchen(
            caller,
            "ALOHA-CHI-001",
            kitchen_table_name=KITCHEN_TABLE_NAME,
            table_getter=table_getter,
        )

    assert snapshot is not None
    assert snapshot["propertyId"] == "ALOHA-CHI-001"
    assert snapshot["channelMixNote"] == "note"


def test_get_kitchen_denies_out_of_scope() -> None:
    """A GM cannot read the snapshot for a non-associated property.

    Validates: Requirements 16.6
    """
    with mock_aws():
        table = create_kitchen_table()
        table.put_item(Item=make_kitchen_item("ALOHA-MIA-001"))
        caller = identity("jsmith", {"ALOHA-CHI-001"})

        out_of_scope = repo.get_kitchen(
            caller,
            "ALOHA-MIA-001",
            kitchen_table_name=KITCHEN_TABLE_NAME,
            table_getter=table_getter,
        )

    # Out-of-scope is indistinguishable None (no cross-property leak).
    assert out_of_scope is None


def test_get_kitchen_missing_returns_none() -> None:
    """A missing snapshot for an associated property returns None.

    Validates: Requirements 16.6
    """
    with mock_aws():
        create_kitchen_table()
        caller = identity("jsmith", {"ALOHA-CHI-001"})

        missing = repo.get_kitchen(
            caller,
            "ALOHA-CHI-001",
            kitchen_table_name=KITCHEN_TABLE_NAME,
            table_getter=table_getter,
        )

    assert missing is None


def test_get_kitchen_overlays_live_banquet_countdown() -> None:
    """The returned snapshot's banquet countdown is recomputed live at read time.

    The stored ``minutesRemaining`` is only a fallback; ``get_kitchen`` overlays
    a live value so a snapshot seeded long ago never shows a frozen timer.
    Validates: Requirements 16.6 (read path) + the live-countdown enhancement.
    """
    with mock_aws():
        table = create_kitchen_table()
        item = make_kitchen_item("ALOHA-CHI-001")
        # Stored fallback is 18; the live overlay must be within the rolling
        # window bounds regardless of the stored value.
        item["banquetCountdown"]["minutesRemaining"] = 18
        table.put_item(Item=item)
        caller = identity("jsmith", {"ALOHA-CHI-001"})

        snapshot = repo.get_kitchen(
            caller,
            "ALOHA-CHI-001",
            kitchen_table_name=KITCHEN_TABLE_NAME,
            table_getter=table_getter,
        )

    assert snapshot is not None
    countdown = snapshot["banquetCountdown"]
    # Live value is a plausible countdown within the rolling window (never 0/neg).
    assert 1 <= countdown["minutesRemaining"] <= repo._BANQUET_WINDOW_MINUTES
    assert 1 <= countdown["progressPct"] <= 99


def test_live_banquet_countdown_is_deterministic_for_a_fixed_time() -> None:
    """The live countdown is a pure function of the stored payload and ``now``."""
    stored = {
        "title": "Gala \u00b7 90 Covers",
        "minutesRemaining": 5,
        "progressPct": 40,
    }
    # 12 minutes into a 45-minute window -> 33 remaining.
    now = datetime(2026, 8, 21, 10, 12, 0, tzinfo=UTC)

    live = repo.live_banquet_countdown(stored, now)

    assert live is not None
    assert live["minutesRemaining"] == 33
    assert live["badge"] == "On Track"
    assert "Gala" in live["subline"]
    # The source payload is not mutated (pure overlay).
    assert stored["minutesRemaining"] == 5


def test_live_banquet_countdown_passes_through_none() -> None:
    """A snapshot with no banquet countdown yields None (no synthetic card)."""
    now = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)
    assert repo.live_banquet_countdown(None, now) is None
