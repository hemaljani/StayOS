"""Property test for the pure property-isolation filter of the Signal Reader.

Covers ``forecast.signals.filter_to_property`` - the PURE, deterministic core of
the property-isolation boundary (Req 6.3, 6.4). Given the raw rows of a
READ-ONLY tool result and the single ``propertyId`` bound to the run, the filter
retains ONLY records whose ``propertyId`` equals the bound id and excludes every
foreign or missing-``propertyId`` record, recording one
:class:`PropertyDiscrepancy` per distinct foreign id (in first-seen order) plus a
synthetic "no matching data" discrepancy when a non-empty response yields zero
matches.

Because the filter is pure (no I/O, no clients, no model), this is exercised
directly with Hypothesis over a mixed-property list of record dicts whose
``propertyId`` is sampled from a small pool: the bound run id, several foreign
ids, and records with NO ``propertyId`` key. The same logical records are also
fed as a bare list AND as a ``{"records": [...]}`` dict envelope to assert the
filter is robust to the raw tool shapes.

Validates: Requirements 6.3, 6.4.
"""

from __future__ import annotations

from typing import Any, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.signals import (
    NO_MATCHING_DATA_REASON,
    PropertyDiscrepancy,
    filter_to_property,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The bound run property id every generated case filters against. Foreign ids are
# drawn from a disjoint pool so no foreign id can accidentally equal the run id.
RUN_PROPERTY_ID = "prop-RUN"

# The pool of ids a generated record's ``propertyId`` is sampled from: the bound
# run id (kept), several foreign ids (excluded), and ``None`` as a sentinel for a
# record that will be emitted WITHOUT a ``propertyId`` key at all (also excluded,
# Req 6.3). ``None`` groups with true missing-key records under a ``None`` foreign
# id inside the filter.
FOREIGN_PROPERTY_IDS = ("prop-A", "prop-B", "prop-C")
PROPERTY_ID_POOL = (RUN_PROPERTY_ID, *FOREIGN_PROPERTY_IDS, None)


def _make_record(property_id: Optional[str], payload: int) -> dict[str, Any]:
    """Build one raw record dict, omitting ``propertyId`` when ``None``.

    Args:
        property_id: The id to stamp under ``propertyId``, or ``None`` to emit a
            record with NO ``propertyId`` key (a missing-id record, Req 6.3).
        payload: An arbitrary non-key field so records are distinguishable and the
            filter is seen to preserve whole rows, not just ids.

    Returns:
        A record dict carrying ``propertyId`` (unless ``None``) plus a ``value``.
    """
    record: dict[str, Any] = {"value": payload}
    if property_id is not None:
        # Only attach the key when an id is present; a None id models a record
        # that arrives WITHOUT any propertyId key at all.
        record["propertyId"] = property_id
    return record


# A single generated record: an id sampled from the pool (including None -> no
# key) paired with a distinguishing payload value.
_record_strategy = st.builds(
    _make_record,
    property_id=st.sampled_from(PROPERTY_ID_POOL),
    payload=st.integers(min_value=0, max_value=1000),
)


def _effective_foreign_id(record: dict[str, Any]) -> Optional[str]:
    """Return the id the filter groups a foreign/missing record under.

    A record with no ``propertyId`` key is grouped under ``None`` (matching the
    filter's own treatment of a missing id, Req 6.3).

    Args:
        record: A raw record dict that did NOT match the run id.

    Returns:
        The record's ``propertyId`` value, or ``None`` when the key is absent.
    """
    return record.get("propertyId")


# ---------------------------------------------------------------------------
# Property 26: Property-isolation filter retains only matching records
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 26: Property-isolation filter retains only matching records and records discrepancies
@PROPERTY_SETTINGS
@given(records=st.lists(_record_strategy, min_size=0, max_size=40))
def test_property_26_isolation_filter_retains_only_matching(
    records: list[dict[str, Any]],
) -> None:
    """Isolation filter keeps only run-id records and records discrepancies.

    Asserts, for any mixed-property list of raw record dicts filtered against the
    bound run id, and independent of the raw shape it arrives in:
    - ``kept_records`` are EXACTLY the records whose ``propertyId`` equals the run
      id, order preserved (Req 6.3);
    - no kept record carries a foreign or missing ``propertyId`` (Req 6.3);
    - each distinct foreign (or missing/``None``) ``propertyId`` yields exactly
      one :class:`PropertyDiscrepancy` with the matching ``foreign_property_id``
      and an ``excluded_count`` equal to how many such records were dropped, and
      these foreign discrepancies appear in first-seen order (Req 6.3);
    - when the input had records but NONE matched, ``kept_records`` is empty and a
      discrepancy with ``reason`` == :data:`NO_MATCHING_DATA_REASON` is present
      (Req 6.4);
    - when the input is empty, ``kept_records`` == ``[]`` and no discrepancy is
      recorded (nothing to isolate);
    - feeding the same logical records as a bare list, as a
      ``{"records": [...]}`` envelope, and (empty case) as ``None`` yields
      identical outcomes (robustness to raw shapes).
    """
    # Expected kept records: exactly the run-id records, order preserved.
    expected_kept = [
        record for record in records if record.get("propertyId") == RUN_PROPERTY_ID
    ]

    # Expected foreign tally in first-seen order (missing key grouped under None).
    expected_foreign_order: list[Optional[str]] = []
    expected_foreign_counts: dict[Optional[str], int] = {}
    for record in records:
        if record.get("propertyId") == RUN_PROPERTY_ID:
            continue
        foreign_id = _effective_foreign_id(record)
        if foreign_id not in expected_foreign_counts:
            expected_foreign_counts[foreign_id] = 0
            expected_foreign_order.append(foreign_id)
        expected_foreign_counts[foreign_id] += 1

    # The filter must produce identical outcomes for the bare-list and the
    # dict-envelope shapes of the same logical records (robustness, task 5.3).
    kept_from_list, discrepancies_from_list = filter_to_property(
        list(records), RUN_PROPERTY_ID
    )
    kept_from_envelope, discrepancies_from_envelope = filter_to_property(
        {"records": list(records)}, RUN_PROPERTY_ID
    )

    assert kept_from_list == kept_from_envelope
    assert discrepancies_from_list == discrepancies_from_envelope

    kept_records = kept_from_list
    discrepancies = discrepancies_from_list

    # kept_records are EXACTLY the run-id records, order preserved (Req 6.3).
    assert kept_records == expected_kept

    # No kept record carries a foreign or missing propertyId (Req 6.3).
    for record in kept_records:
        assert record.get("propertyId") == RUN_PROPERTY_ID

    # Split the recorded discrepancies into the foreign-record entries and the
    # synthetic "no matching data" entry (Req 6.4) so each can be asserted apart.
    foreign_discrepancies = [
        discrepancy
        for discrepancy in discrepancies
        if discrepancy.reason is None
    ]
    no_match_discrepancies = [
        discrepancy
        for discrepancy in discrepancies
        if discrepancy.reason == NO_MATCHING_DATA_REASON
    ]

    # Exactly one foreign discrepancy per distinct foreign id, in first-seen order,
    # each carrying the right foreign id and excluded_count (Req 6.3).
    assert [d.foreign_property_id for d in foreign_discrepancies] == (
        expected_foreign_order
    )
    for discrepancy in foreign_discrepancies:
        assert discrepancy.run_property_id == RUN_PROPERTY_ID
        assert discrepancy.excluded_count == (
            expected_foreign_counts[discrepancy.foreign_property_id]
        )
        # Foreign entries carry no reason (that is reserved for the zero-match case).
        assert discrepancy.reason is None

    # Every recorded discrepancy is one of the two expected kinds - nothing else.
    assert len(foreign_discrepancies) + len(no_match_discrepancies) == len(
        discrepancies
    )

    if not records:
        # Empty input: nothing kept, nothing to isolate (no discrepancies).
        assert kept_records == []
        assert discrepancies == ()
    elif not expected_kept:
        # Non-empty response, zero matches: empty kept list plus a dedicated
        # "no matching data" discrepancy (Req 6.4).
        assert kept_records == []
        assert len(no_match_discrepancies) == 1
        no_match = no_match_discrepancies[0]
        assert no_match.reason == NO_MATCHING_DATA_REASON
        assert no_match.run_property_id == RUN_PROPERTY_ID
        assert no_match.foreign_property_id is None
        assert no_match.excluded_count == len(records)
    else:
        # At least one match: no "no matching data" discrepancy is recorded.
        assert no_match_discrepancies == []

    # Every foreign discrepancy is a PropertyDiscrepancy (type contract).
    for discrepancy in discrepancies:
        assert isinstance(discrepancy, PropertyDiscrepancy)


# Feature: predictive-forecasting-agent, Property 26: Property-isolation filter retains only matching records and records discrepancies
def test_property_26_empty_and_none_inputs_isolate_nothing() -> None:
    """Empty list and ``None`` inputs keep nothing and record no discrepancy.

    An absent/empty response has nothing to isolate, so both a bare empty list and
    ``None`` (the "tool returned no data" shape) must yield ``([], ())``.
    """
    kept_empty, discrepancies_empty = filter_to_property([], RUN_PROPERTY_ID)
    assert kept_empty == []
    assert discrepancies_empty == ()

    kept_none, discrepancies_none = filter_to_property(None, RUN_PROPERTY_ID)
    assert kept_none == []
    assert discrepancies_none == ()
