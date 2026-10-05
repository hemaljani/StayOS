"""Property-based tests for window-relative, idempotent dataset generation.

These tests cover the Correctness Properties defined in the data-Orchestrator
design for Task 1 (make generation window-relative and idempotent). They drive
the deterministic generators with arbitrary ``reference_date`` values and assert
the invariants that must hold for any anchor date:

- Property 1: the generated 30-day window is contiguous, and every reservation's
  arrival/departure/status relationship is valid relative to the reference date.
- Property 2: re-running generation with the same reference date yields identical
  items, and the ``BatchWriter`` idempotent-upsert path treats an unchanged
  re-run as a no-op (put-if-changed, never delete).
- Property 3: a given reference date always produces byte-identical items.
- Property 6: the per-table item count is a fixed function of the window size and
  does not grow with the reference date (the volume invariant).

External boundaries (DynamoDB) are mocked; no AWS calls are made. Hypothesis is
capped at a small number of examples with no deadline to keep runs fast.
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from dataset_generator.config import PROPERTY_PROFILES, SEED_DAYS
from dataset_generator.reservations_generator import _determine_status
from dataset_generator.revenue_generator import generate_revenue

# Reference dates are drawn from a bounded, realistic range so the deterministic
# offset tables (occupancy/ADR) index cleanly while still exercising many anchors.
_REFERENCE_DATE_STRATEGY = st.dates(
    min_value=date(2024, 1, 1),
    max_value=date(2030, 12, 31),
)

# The revenue generator emits SEED_DAYS historical days plus today and tomorrow.
_WINDOW_DAYS: int = SEED_DAYS + 2

# Valid reservation status values produced by _determine_status.
_VALID_STATUSES = frozenset(
    {"CONFIRMED", "CHECKED_IN", "CHECKED_OUT", "NO_SHOW", "CANCELLED"}
)


def _make_mock_writer() -> MagicMock:
    """Build a mock BatchWriter that records writes without touching AWS.

    Returns:
        A MagicMock whose ``write_items`` returns a plausible success summary,
        standing in for the real DynamoDB-backed BatchWriter.
    """
    writer = MagicMock()
    writer.write_items.return_value = {"success": 0, "failed": 0, "skipped": 0}
    return writer


def _dates_for_property(
    result: Dict[Tuple[str, str], Dict[str, Any]], property_id: str
) -> List[date]:
    """Extract and sort the generated dates for one property.

    Args:
        result: The revenue lookup keyed by (propertyId, date_str).
        property_id: The property whose dates to collect.

    Returns:
        Sorted list of ``date`` objects for that property's window.
    """
    return sorted(
        date.fromisoformat(key[1])
        for key in result
        if key[0] == property_id
    )


# ---------------------------------------------------------------------------
# Property 1: window contiguous and status-valid
# ---------------------------------------------------------------------------

# Feature: data-Orchestrator, Property 1: window contiguous and status-valid
@settings(max_examples=25, deadline=None)
@given(reference_date=_REFERENCE_DATE_STRATEGY)
def test_window_contiguous_and_status_valid(reference_date: date) -> None:
    """The generated window is contiguous and reservation statuses are valid.

    For any reference_date the per-property window must be a contiguous run of
    days (no gap, no overlap) anchored on reference_date, and the reservation
    status derived from any arrival/departure pair must be a valid status that
    is consistent with the date relationship (Requirement 2.2).

    **Validates: Requirements 2.2**
    """
    writer = _make_mock_writer()
    result = generate_revenue(writer, reference_date=reference_date)

    expected_start = reference_date - timedelta(days=SEED_DAYS - 1)

    for profile in PROPERTY_PROFILES:
        dates = _dates_for_property(result, profile["propertyId"])
        # Exactly the expected number of days, contiguous, correctly anchored.
        assert len(dates) == _WINDOW_DAYS
        assert dates[0] == expected_start
        assert dates[-1] == reference_date + timedelta(days=2)
        for earlier, later in zip(dates, dates[1:]):
            # Contiguous: each day is exactly one after the previous (no gap/overlap).
            assert (later - earlier).days == 1

    # Reservation arrival/departure/status relationships are valid for any pair
    # of dates within the window relative to the reference date.
    for day_offset in range(_WINDOW_DAYS):
        arrival = expected_start + timedelta(days=day_offset)
        for stay_nights in (1, 3, 7):
            departure = arrival + timedelta(days=stay_nights)
            for reservation_index in range(3):
                status = _determine_status(
                    arrival, departure, reference_date, reservation_index
                )
                assert status in _VALID_STATUSES
                assert departure > arrival
                if status == "CONFIRMED":
                    assert arrival > reference_date
                elif status == "CHECKED_OUT":
                    assert departure <= reference_date
                elif status == "CHECKED_IN":
                    # Either arriving today, or arrived in the past and still in-hotel.
                    assert arrival <= reference_date < departure or arrival == reference_date


# ---------------------------------------------------------------------------
# Property 2: re-run with same reference_date is a no-op
# ---------------------------------------------------------------------------

# Feature: data-Orchestrator, Property 2: re-run with same reference_date is a no-op
@settings(max_examples=25, deadline=None)
@given(reference_date=_REFERENCE_DATE_STRATEGY)
def test_rerun_same_reference_date_is_noop(reference_date: date) -> None:
    """Two runs with the same reference_date produce identical items.

    Re-anchoring twice to the same date must yield exactly the same generated
    items, which is the precondition for the idempotent-upsert write path to be
    a genuine no-op on the second run (Requirements 2.3, 2.4).

    **Validates: Requirements 2.3, 2.4**
    """
    first = generate_revenue(_make_mock_writer(), reference_date=reference_date)
    second = generate_revenue(_make_mock_writer(), reference_date=reference_date)
    assert first == second


# Feature: data-Orchestrator, Property 2: re-run with same reference_date is a no-op
@settings(max_examples=25, deadline=None)
@given(reference_date=_REFERENCE_DATE_STRATEGY)
def test_idempotent_upsert_skips_unchanged_items(reference_date: date) -> None:
    """Idempotent upsert of an already-stored dataset writes nothing.

    With the DynamoDB boundary mocked so every generated item already exists
    unchanged, the BatchWriter idempotent-upsert path must skip all items and
    perform no PutRequest, proving a same-date re-run causes no net change and
    never deletes (Requirements 2.3, 2.4).

    **Validates: Requirements 2.3, 2.4**
    """
    from dataset_generator import writer as writer_module

    items = list(
        generate_revenue(_make_mock_writer(), reference_date=reference_date).values()
    )

    batch_writer = writer_module.BatchWriter("stayos-revenues-test")
    # Table has a composite key of propertyId (partition) + date (sort).
    batch_writer._key_attributes = ["propertyId", "date"]

    def _fake_batch_get_existing(
        converted: List[Dict[str, Any]], key_attributes: List[str]
    ) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
        """Return every item as already stored, byte-identical."""
        return {
            batch_writer._item_key_tuple(item, key_attributes): item
            for item in converted
        }

    write_batch_calls: List[List[Dict[str, Any]]] = []

    def _record_write_batch(chunk: List[Dict[str, Any]], chunk_index: int) -> None:
        """Record any batch that would be written so we can assert none occur."""
        write_batch_calls.append(chunk)

    # Patch the read-back and the low-level batch write on this instance only.
    batch_writer._batch_get_existing = _fake_batch_get_existing  # type: ignore[method-assign]
    batch_writer._write_batch = _record_write_batch  # type: ignore[method-assign]

    summary = batch_writer.write_items(items, idempotent=True)

    # No item written, every item skipped, nothing failed -> a true no-op.
    assert write_batch_calls == []
    assert summary["success"] == 0
    assert summary["failed"] == 0
    assert summary["skipped"] == len(items)


# ---------------------------------------------------------------------------
# Property 3: deterministic output
# ---------------------------------------------------------------------------

# Feature: data-Orchestrator, Property 3: deterministic output
@settings(max_examples=25, deadline=None)
@given(reference_date=_REFERENCE_DATE_STRATEGY)
def test_deterministic_output(reference_date: date) -> None:
    """A given reference_date always yields byte-identical items.

    Generation must be fully deterministic (no randomness), so repeated runs
    with the same anchor produce structurally identical items down to every
    attribute value (Requirement 2.5).

    **Validates: Requirements 2.5**
    """
    first = generate_revenue(_make_mock_writer(), reference_date=reference_date)
    second = generate_revenue(_make_mock_writer(), reference_date=reference_date)

    assert first.keys() == second.keys()
    for key in first:
        # Equality over dicts compares every attribute value byte-for-byte
        # (Decimal, str, int) - a stricter check than key equality alone.
        assert first[key] == second[key]


# ---------------------------------------------------------------------------
# Property 6: volume invariant
# ---------------------------------------------------------------------------

# Feature: data-Orchestrator, Property 6: volume invariant
@settings(max_examples=25, deadline=None)
@given(reference_date=_REFERENCE_DATE_STRATEGY)
def test_volume_invariant(reference_date: date) -> None:
    """Item count is a fixed function of window size, independent of the date.

    Roll-forward overwrites in place and must not cause unbounded growth: the
    per-property day count and the total item count stay constant for any
    reference_date (Requirement 2.3 and the data-volume invariant).

    **Validates: Requirements 2.3**
    """
    result = generate_revenue(_make_mock_writer(), reference_date=reference_date)

    assert len(result) == len(PROPERTY_PROFILES) * _WINDOW_DAYS
    for profile in PROPERTY_PROFILES:
        prop_items = [
            key for key in result if key[0] == profile["propertyId"]
        ]
        assert len(prop_items) == _WINDOW_DAYS
