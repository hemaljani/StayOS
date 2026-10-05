"""Property test for predictive-alert written-record payload completeness.

Covers the Alert Writer's pure record-assembly step
(``forecast.alerts.build_forecast_alert_item``): the assembled ``pulse-alerts``
item must be a ``FORECAST_OVERSELL`` alert whose ``forecast`` sub-object carries
every fact a general manager needs to act on a predicted oversell -- the
anticipated date, the condition type, the confidence, the deterministic
rooms-oversold count, and the advisory recommendation (narrative + steps, with
an overflow option only when a sister-property candidate exists), plus the
partial-data flag.

Assembly is pure (no DynamoDB), so it is exercised directly with Hypothesis over
a generated property id, rooms-oversold count, confidence, ISO anticipated date,
and an optional overflow candidate. The RemediationPlan is the deterministic
TEMPLATE plan authored with no model configured (``model_id=None``), and the
dedupeKey is built by ``derive_dedupe_key``.

Validates: Requirements 4.7.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    DEFAULT_CONDITION_TYPE,
    FORECAST_ALERT_TYPE,
    build_forecast_alert_item,
    derive_dedupe_key,
)
from forecast.narrative import author_remediation_plan

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# A fixed threshold: the payload shape is independent of the gate, and confidence
# is generated across the whole [0, 100] band, so any constant threshold is fine.
THRESHOLD: int = 50

# Non-empty token strategy for ids (no whitespace-only tokens, which the
# dedupeKey derivation would reject).
_TOKENS = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=24,
).filter(lambda token: token.strip() != "")


# ---------------------------------------------------------------------------
# Property 20: Written record payload completeness
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 20: Written record contains anticipated date, condition, confidence, and recommendation
@PROPERTY_SETTINGS
@given(
    property_id=_TOKENS,
    rooms_oversold=st.integers(min_value=1, max_value=50),
    confidence=st.integers(min_value=0, max_value=100),
    anticipated_date=st.dates(
        min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)
    ),
    based_on_partial_data=st.booleans(),
    # Optional overflow candidate: either a (sister_id, available_rooms) pair or
    # None, so both the "overflow present" and "overflow absent" branches of the
    # rendered recommendation are exercised.
    overflow=st.one_of(
        st.none(),
        st.tuples(_TOKENS, st.integers(min_value=1, max_value=50)),
    ),
)
def test_property_20_written_record_payload_completeness(
    property_id: str,
    rooms_oversold: int,
    confidence: int,
    anticipated_date: date,
    based_on_partial_data: bool,
    overflow: tuple[str, int] | None,
) -> None:
    """Assembled item carries date, condition, confidence, and recommendation.

    Asserts, for any property/count/confidence/date and either overflow branch,
    that the assembled item is a ``FORECAST_OVERSELL`` alert whose ``forecast``
    sub-object reproduces the anticipated date (normalized to ``YYYY-MM-DD``),
    the ``OVERSELL`` condition, the confidence and rooms-oversold count, the
    advisory recommendation (narrative + steps + roomsOversold, with an
    overflowOption only when a sister candidate exists), and the partial-data
    flag.
    """
    iso_date = anticipated_date.isoformat()
    overflow_sister_property_id = overflow[0] if overflow is not None else None
    overflow_available_rooms = overflow[1] if overflow is not None else None

    # Deterministic TEMPLATE plan (no model configured) carrying the fixed facts.
    plan = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=iso_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        model_id=None,
    )

    dedupe_key = derive_dedupe_key(property_id, DEFAULT_CONDITION_TYPE, iso_date)

    item = build_forecast_alert_item(
        property_id=property_id,
        plan=plan,
        rooms_oversold=rooms_oversold,
        anticipated_date=iso_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=based_on_partial_data,
        threshold=THRESHOLD,
    )

    # Top-level type marks this as a forecast-originated oversell alert.
    assert item["type"] == FORECAST_ALERT_TYPE == "FORECAST_OVERSELL"

    forecast = item["forecast"]

    # The deterministic facts are reproduced verbatim on the forecast sub-object.
    assert forecast["anticipatedDate"] == iso_date
    assert forecast["conditionType"] == "OVERSELL"
    assert forecast["confidence"] == confidence
    assert forecast["roomsOversold"] == rooms_oversold
    assert forecast["basedOnPartialData"] == based_on_partial_data

    # The advisory recommendation exists and carries narrative + steps + count.
    recommendation = forecast["recommendation"]
    assert isinstance(recommendation["narrative"], str)
    assert isinstance(recommendation["steps"], list)
    assert recommendation["roomsOversold"] == rooms_oversold

    # The overflow option is present only when a sister candidate was provided.
    if overflow is not None:
        overflow_option = recommendation["overflowOption"]
        assert overflow_option["sisterPropertyId"] == overflow_sister_property_id
        assert overflow_option["availableRooms"] == overflow_available_rooms
    else:
        assert "overflowOption" not in recommendation
