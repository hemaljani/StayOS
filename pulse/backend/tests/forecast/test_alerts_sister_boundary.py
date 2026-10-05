"""Property test: only sister-property availability crosses the property boundary.

Covers the pure cross-property projection in the Signal Reader
(``forecast.signals.project_sister_availability``). Sister-property availability
is the SOLE cross-property datum a forecasting run may admit for overflow
evaluation, and even then only the availability *value* may cross the boundary:
every other operational field belonging to a sister ``propertyId`` (occupancy,
revenue, guest lists, ADR, arbitrary secret fields) MUST be discarded (Req 6.5,
6.6). The projection also skips the bound run property (a property does not
overflow to itself) and any record missing a ``propertyId`` or an availability
value.

Because the projection is pure (no I/O, no clients, no model), it is exercised
directly with Hypothesis over synthetic sister records built with a mix of
foreign sister ids, the bound run id, and missing/``None`` ids - each carrying an
availability value plus several other operational fields whose values must never
leak into the projected mapping.

Validates: Requirements 6.5, 6.6.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.signals import (
    SISTER_AVAILABILITY_VALUE_KEYS,
    project_sister_availability,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The single bound run property every generated batch is filtered against. A
# stable literal keeps the "skip the bound property" assertion unambiguous.
RUN_PROPERTY_ID = "ALOHA-CHI-001"

# Foreign sister property ids the projection MAY admit (never the run id).
FOREIGN_SISTER_IDS: tuple[str, ...] = (
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)

# The other operational fields that must NEVER cross the property boundary: only
# availability is admitted, so none of these keys' values may appear in the
# projected mapping (Req 6.5, 6.6).
OTHER_OPERATIONAL_KEYS: tuple[str, ...] = (
    "occupancy",
    "revenue",
    "guestList",
    "adr",
    "secretField",
)


# A propertyId is sampled from the foreign sisters, the bound run id, or a
# missing/None marker so every branch of the projection is exercised.
_PROPERTY_ID_CHOICES = st.sampled_from(
    [*FOREIGN_SISTER_IDS, RUN_PROPERTY_ID, None]
)

# Availability values are arbitrary JSON-ish scalars; the projection copies the
# value through verbatim, so we keep them simple and comparable.
_AVAILABILITY_VALUES = st.one_of(
    st.integers(min_value=0, max_value=500),
    st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)


@st.composite
def _sister_record(draw: st.DrawFn) -> dict[str, Any]:
    """Build one synthetic sister record for the projection.

    Each record carries: an optionally-present ``propertyId`` (foreign / run /
    missing); exactly one availability field keyed by a member of
    :data:`SISTER_AVAILABILITY_VALUE_KEYS` (sometimes omitted so the "missing
    availability" branch is covered); and several OTHER operational fields with
    arbitrary values that must never cross the boundary.

    Args:
        draw: Hypothesis' draw callable.

    Returns:
        A single record dict shaped like a raw ``get_sister_property_availability``
        row.
    """
    record: dict[str, Any] = {}

    # propertyId: foreign sister, the bound run id, or missing entirely (None).
    property_id = draw(_PROPERTY_ID_CHOICES)
    if property_id is not None:
        record["propertyId"] = property_id

    # Availability value under one of the recognized keys - sometimes omitted so
    # the "no availability -> skip" branch is exercised.
    include_availability = draw(st.booleans())
    if include_availability:
        availability_key = draw(st.sampled_from(list(SISTER_AVAILABILITY_VALUE_KEYS)))
        record[availability_key] = draw(_AVAILABILITY_VALUES)

    # Several OTHER operational fields with arbitrary values. None of these may
    # leak into the projected mapping (Req 6.5, 6.6).
    for other_key in OTHER_OPERATIONAL_KEYS:
        if draw(st.booleans()):
            record[other_key] = draw(
                st.one_of(
                    st.integers(),
                    st.text(max_size=6),
                    st.lists(st.integers(), max_size=3),
                    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
                )
            )

    return record


def _availability_of(record: dict[str, Any]) -> Any:
    """Return the record's availability value, probing the recognized keys in order.

    Mirrors the projection's own key precedence so the test can independently
    compute the value it expects to have crossed the boundary.

    Args:
        record: A single sister record dict.

    Returns:
        The availability value under the first present key in
        :data:`SISTER_AVAILABILITY_VALUE_KEYS`, or a sentinel-free ``None`` when
        no availability key is present.
    """
    for value_key in SISTER_AVAILABILITY_VALUE_KEYS:
        if value_key in record:
            return record[value_key]
    return None


def _collect_leaked_values(value: Any) -> set[Any]:
    """Flatten a projected value into the set of hashable scalars it contains.

    Used to prove that no OTHER operational field's value leaked: a correctly
    projected value is the sister's scalar availability value, never a nested
    dict / list carrying the rest of the record. Recurses through lists and dict
    values so any structural leak is caught.

    Args:
        value: A single projected mapping value.

    Returns:
        The set of hashable leaf values reachable from ``value``.
    """
    leaves: set[Any] = set()
    if isinstance(value, dict):
        for nested in value.values():
            leaves |= _collect_leaked_values(nested)
    elif isinstance(value, list):
        for nested in value:
            leaves |= _collect_leaked_values(nested)
    else:
        try:
            leaves.add(value)
        except TypeError:
            # Unhashable leaf: ignore for membership checks (still not a scalar
            # availability value, which the scalar assertion catches separately).
            pass
    return leaves


def _assert_only_availability_crosses(
    records: list[dict[str, Any]], projected: dict[str, Any]
) -> None:
    """Assert the projected mapping admits only foreign sisters' availability.

    Shared assertion body for both the bare-list and envelope input shapes.

    Args:
        records: The synthetic sister records fed to the projection.
        projected: The mapping returned by :func:`project_sister_availability`.
    """
    # Keys are only FOREIGN sister ids - never the run id, never None.
    assert RUN_PROPERTY_ID not in projected
    assert None not in projected
    for sister_id in projected:
        assert sister_id in FOREIGN_SISTER_IDS

    # For each retained sister, the value equals ONLY that sister's availability
    # value (last-writer-wins over the record order, matching a dict build), and
    # is a scalar - never a dict of the whole record.
    expected: dict[str, Any] = {}
    for record in records:
        sister_id = record.get("propertyId")
        if sister_id is None or sister_id == RUN_PROPERTY_ID:
            continue
        availability = _availability_of(record)
        if availability is None and not any(
            key in record for key in SISTER_AVAILABILITY_VALUE_KEYS
        ):
            # No availability value present: this record is skipped entirely.
            continue
        expected[sister_id] = availability

    assert projected == expected

    # No OTHER operational field's value may appear anywhere in the projection.
    # Gather every other-field value that belonged to an admitted sister and
    # confirm none of them leaked through as a projected value.
    for sister_id, projected_value in projected.items():
        # Each projected value is exactly the availability scalar, not a record.
        assert not isinstance(projected_value, (dict, list))

    leaked = set()
    for projected_value in projected.values():
        leaked |= _collect_leaked_values(projected_value)

    # Every OTHER operational value that ANY sister carried must be absent from
    # the projected values, unless it happens to equal a legitimate availability
    # value - so we only flag values that are NOT any admitted availability.
    admitted_availability_values = set()
    for sister_id in projected:
        avail = projected[sister_id]
        try:
            admitted_availability_values.add(avail)
        except TypeError:
            pass

    for record in records:
        sister_id = record.get("propertyId")
        if sister_id is None or sister_id == RUN_PROPERTY_ID:
            continue
        for other_key in OTHER_OPERATIONAL_KEYS:
            if other_key not in record:
                continue
            for other_leaf in _collect_leaked_values(record[other_key]):
                if other_leaf in admitted_availability_values:
                    # Value coincidentally equals a real availability value;
                    # its presence is not evidence of a leak.
                    continue
                assert other_leaf not in leaked


# Feature: predictive-forecasting-agent, Property 27: Only sister-property availability crosses the property boundary
@PROPERTY_SETTINGS
@given(records=st.lists(_sister_record(), max_size=12))
def test_property_27_only_availability_crosses_bare_list(
    records: list[dict[str, Any]],
) -> None:
    """Bare-list input: only foreign sisters' availability values cross over.

    Asserts, for any list of sister records fed as a bare list:
    - the mapping keys are only FOREIGN sister ids (never the run id, never None);
    - each retained sister's value is exactly that sister's availability value
      (a scalar), not a dict of the whole record;
    - none of the OTHER operational fields (occupancy / revenue / guestList / adr
      / secretField) leak into any projected value (Req 6.5, 6.6);
    - bound run-property records, and records missing a ``propertyId`` or an
      availability value, are skipped.
    """
    projected = project_sister_availability(records, RUN_PROPERTY_ID)
    _assert_only_availability_crosses(records, projected)


# Feature: predictive-forecasting-agent, Property 27: Only sister-property availability crosses the property boundary
@PROPERTY_SETTINGS
@given(records=st.lists(_sister_record(), max_size=12))
def test_property_27_only_availability_crosses_envelope(
    records: list[dict[str, Any]],
) -> None:
    """Envelope input: the ``{"records": [...]}`` shape yields the same projection.

    Same guarantees as the bare-list case (Req 6.5, 6.6), proving the projection
    is robust to the dict-envelope raw tool shape as well: only foreign sisters'
    availability values cross the boundary, and no other operational field leaks.
    """
    envelope = {"records": records}
    projected = project_sister_availability(envelope, RUN_PROPERTY_ID)
    _assert_only_availability_crosses(records, projected)
