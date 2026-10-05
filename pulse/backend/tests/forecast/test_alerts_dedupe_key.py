"""Property test for the pure Alert Writer dedupeKey derivation.

Covers the ``dedupeKey`` derivation step of the Alert Writer
(``forecast.alerts.derive_dedupe_key``): the key is a deterministic function of
the property identifier, condition type, and the anticipated date normalized to
a single local calendar day, emitted in the literal spec form
``forecast#{propertyId}#{CONDITION}#{YYYY-MM-DD}`` (Req 7.1). Because derivation
is pure (no I/O, no clients), this is exercised directly with Hypothesis over a
small property-id pool, a condition token, and anticipated dates expressed as
both ISO ``YYYY-MM-DD`` strings and ISO datetime strings for the SAME calendar
day.

Validates: Requirements 7.1.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import derive_dedupe_key

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# A small pool of property identifiers so distinct/equal property draws are both
# common enough for the distinguishing property to exercise each branch.
PROPERTY_ID_POOL = ["prop-a", "prop-b", "prop-c", "prop-d"]

# The condition token used throughout; the emitted key uppercases it (Req 7.1).
CONDITION_TYPE = "OVERSELL"

# A fixed base date; anticipated days are derived as base + offset so a
# YYYY-MM-DD string and a datetime string can target the SAME calendar day.
BASE_DATE = date(2025, 6, 1)


def _day_for_offset(offset: int) -> date:
    """Return the anticipated calendar day for a day offset from the base date."""
    return BASE_DATE + timedelta(days=offset)


# ---------------------------------------------------------------------------
# Property 17: dedupeKey derivation is deterministic and input-distinguishing
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 17: dedupeKey derivation is deterministic and input-distinguishing
@PROPERTY_SETTINGS
@given(
    property_id=st.sampled_from(PROPERTY_ID_POOL),
    day_offset=st.integers(min_value=0, max_value=13),
    # An arbitrary time-of-day used to build a datetime string for the SAME day.
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
    second=st.integers(min_value=0, max_value=59),
)
def test_property_17_determinism_and_normalization(
    property_id: str, day_offset: int, hour: int, minute: int, second: int
) -> None:
    """Deterministic, same-day-normalizing, well-formed key.

    Asserts, for any pooled property, condition, and anticipated day:
    - Determinism: calling twice with identical inputs yields an equal key;
    - Same-day normalization: a date-only ISO string and a datetime ISO string
      for the SAME calendar day produce the SAME key (time component dropped);
    - Format: the key starts with ``forecast#`` and contains the propertyId, the
      uppercased ``OVERSELL`` token, and the ``YYYY-MM-DD`` day.
    """
    anticipated_day = _day_for_offset(day_offset)
    date_only = anticipated_day.isoformat()
    datetime_string = f"{date_only}T{hour:02d}:{minute:02d}:{second:02d}"

    # Determinism: same inputs -> same key across repeated calls.
    key_first = derive_dedupe_key(property_id, CONDITION_TYPE, date_only)
    key_second = derive_dedupe_key(property_id, CONDITION_TYPE, date_only)
    assert key_first == key_second

    # Same-day normalization: date-only and datetime-for-same-day collapse to one
    # key because the time component is dropped (Req 7.1).
    key_from_datetime = derive_dedupe_key(property_id, CONDITION_TYPE, datetime_string)
    assert key_from_datetime == key_first

    # Format: literal spec shape forecast#{propertyId}#OVERSELL#{YYYY-MM-DD}.
    assert key_first.startswith("forecast#")
    assert property_id in key_first
    assert "OVERSELL" in key_first
    assert date_only in key_first


# Feature: predictive-forecasting-agent, Property 17: dedupeKey derivation is deterministic and input-distinguishing
@PROPERTY_SETTINGS
@given(
    property_id_one=st.sampled_from(PROPERTY_ID_POOL),
    property_id_two=st.sampled_from(PROPERTY_ID_POOL),
    day_offset_one=st.integers(min_value=0, max_value=13),
    day_offset_two=st.integers(min_value=0, max_value=13),
)
def test_property_17_input_distinguishing(
    property_id_one: str,
    property_id_two: str,
    day_offset_one: int,
    day_offset_two: int,
) -> None:
    """Keys differ IFF (propertyId, normalized day) differ, condition constant.

    Holding the condition token constant, two derivations produce different keys
    exactly when their property id or their normalized anticipated calendar day
    differs, and produce the same key when both match (Req 7.1). Inputs are
    supplied as date-only ISO strings so the normalized day equals the drawn day.
    """
    day_one = _day_for_offset(day_offset_one)
    day_two = _day_for_offset(day_offset_two)

    key_one = derive_dedupe_key(property_id_one, CONDITION_TYPE, day_one.isoformat())
    key_two = derive_dedupe_key(property_id_two, CONDITION_TYPE, day_two.isoformat())

    inputs_differ = (property_id_one, day_one) != (property_id_two, day_two)
    assert (key_one != key_two) == inputs_differ
