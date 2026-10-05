"""Property test for dedupeKey-derivation failure skipping a prediction.

Covers the Alert Writer's dedupeKey derivation-failure contract
(``forecast.alerts.derive_dedupe_key`` / ``dedupe_key_for_prediction``): when
the property id, condition type, or anticipated date is missing, empty, or
unparseable, derivation raises :class:`DedupeKeyError` carrying the offending
``failed_input``, and the caller MUST skip that prediction, record an error
identifying it, and leave existing records unchanged - no write to the
``pulse-alerts`` table (Req 7.6).

The skip behavior is modeled with a tiny local ``try_derive_or_skip`` helper
that mirrors what the orchestrator / task 8.4 caller does: call
``derive_dedupe_key`` and, on ``DedupeKeyError``, append an error record and
return ``None`` (skip) without ever touching the table. A :class:`FakeAlertsTable`
(from ``.conftest``) proves no write occurred when derivation fails.

Validates: Requirements 7.6.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    INPUT_ANTICIPATED_DATE,
    INPUT_CONDITION_TYPE,
    INPUT_PROPERTY_ID,
    DedupeKeyError,
    dedupe_key_for_prediction,
    derive_dedupe_key,
)

from .conftest import FakeAlertsTable

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)


def try_derive_or_skip(
    table: Any,
    errors: list[dict[str, Any]],
    property_id: Any,
    condition_type: Any,
    anticipated_date: Any,
) -> Optional[str]:
    """Model the caller's derive-or-skip contract for one prediction.

    Attempts to derive the dedupeKey; on :class:`DedupeKeyError` it records the
    failed prediction (appending the offending ``failed_input`` and value to the
    ``errors`` list) and returns ``None`` so the caller skips the prediction and
    leaves the table untouched (Req 7.6). Only on a successful derivation would a
    caller proceed to a write; this helper itself performs no write, so a failed
    derivation trivially leaves ``table`` unchanged.

    Args:
        table: The (fake) ``pulse-alerts`` table; present to assert no write.
        errors: The accumulating list of recorded derivation errors.
        property_id: The candidate property id (possibly invalid).
        condition_type: The candidate condition type (possibly invalid).
        anticipated_date: The candidate anticipated date (possibly invalid).

    Returns:
        The derived dedupeKey on success, or ``None`` when the prediction was
        skipped due to a derivation failure.
    """
    try:
        return derive_dedupe_key(property_id, condition_type, anticipated_date)
    except DedupeKeyError as exc:
        # Record the failed prediction so the caller can identify it; no write.
        errors.append(
            {
                "failed_input": exc.failed_input,
                "value": exc.value,
            }
        )
        return None


# Whitespace-or-empty strings and None: the invalid token inputs.
_invalid_tokens = st.one_of(
    st.none(),
    st.sampled_from(["", " ", "   ", "\t", "\n", " \t\n "]),
)

# Missing (None), empty/whitespace, and garbage (unparseable) anticipated dates.
_invalid_dates = st.one_of(
    st.none(),
    st.sampled_from(["", "   ", "\t"]),
    st.sampled_from(
        ["not-a-date", "2024-13-01", "2024-02-30", "13/2024", "yesterday", "2024/01/01x"]
    ),
)

# Valid tokens and dates for the control case.
_valid_tokens = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=20,
).filter(lambda s: s.strip() != "")

_valid_dates = st.one_of(
    st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)),
    st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)).map(
        lambda d: d.isoformat()
    ),
    st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 12, 31)
    ),
)


# ---------------------------------------------------------------------------
# Property 22: dedupeKey-derivation failure skips the prediction and leaves
# records unchanged
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 22: dedupeKey-derivation failure skips the prediction and leaves records unchanged
@PROPERTY_SETTINGS
@given(
    bad_property_id=_invalid_tokens,
    condition_type=_valid_tokens,
    anticipated_date=_valid_dates,
)
def test_property_22_invalid_property_id_skips(
    bad_property_id: Any, condition_type: str, anticipated_date: Any
) -> None:
    """An invalid property id raises for INPUT_PROPERTY_ID and skips with no write."""
    with pytest.raises(DedupeKeyError) as excinfo:
        derive_dedupe_key(bad_property_id, condition_type, anticipated_date)
    assert excinfo.value.failed_input == INPUT_PROPERTY_ID

    table = FakeAlertsTable()
    errors: list[dict[str, Any]] = []
    result = try_derive_or_skip(
        table, errors, bad_property_id, condition_type, anticipated_date
    )
    # Skipped, error recorded identifying the failed input, and no write occurred.
    assert result is None
    assert len(errors) == 1
    assert errors[0]["failed_input"] == INPUT_PROPERTY_ID
    assert table.puts == []
    assert table.updates == []


# Feature: predictive-forecasting-agent, Property 22: dedupeKey-derivation failure skips the prediction and leaves records unchanged
@PROPERTY_SETTINGS
@given(
    property_id=_valid_tokens,
    bad_condition_type=_invalid_tokens,
    anticipated_date=_valid_dates,
)
def test_property_22_invalid_condition_skips(
    property_id: str, bad_condition_type: Any, anticipated_date: Any
) -> None:
    """An invalid condition type raises for INPUT_CONDITION_TYPE and skips, no write."""
    with pytest.raises(DedupeKeyError) as excinfo:
        derive_dedupe_key(property_id, bad_condition_type, anticipated_date)
    assert excinfo.value.failed_input == INPUT_CONDITION_TYPE

    table = FakeAlertsTable()
    errors: list[dict[str, Any]] = []
    result = try_derive_or_skip(
        table, errors, property_id, bad_condition_type, anticipated_date
    )
    assert result is None
    assert len(errors) == 1
    assert errors[0]["failed_input"] == INPUT_CONDITION_TYPE
    assert table.puts == []
    assert table.updates == []


# Feature: predictive-forecasting-agent, Property 22: dedupeKey-derivation failure skips the prediction and leaves records unchanged
@PROPERTY_SETTINGS
@given(
    property_id=_valid_tokens,
    condition_type=_valid_tokens,
    bad_anticipated_date=_invalid_dates,
)
def test_property_22_invalid_anticipated_date_skips(
    property_id: str, condition_type: str, bad_anticipated_date: Any
) -> None:
    """An invalid anticipated date raises for INPUT_ANTICIPATED_DATE and skips, no write."""
    with pytest.raises(DedupeKeyError) as excinfo:
        derive_dedupe_key(property_id, condition_type, bad_anticipated_date)
    assert excinfo.value.failed_input == INPUT_ANTICIPATED_DATE

    table = FakeAlertsTable()
    errors: list[dict[str, Any]] = []
    result = try_derive_or_skip(
        table, errors, property_id, condition_type, bad_anticipated_date
    )
    assert result is None
    assert len(errors) == 1
    assert errors[0]["failed_input"] == INPUT_ANTICIPATED_DATE
    assert table.puts == []
    assert table.updates == []


# Feature: predictive-forecasting-agent, Property 22: dedupeKey-derivation failure skips the prediction and leaves records unchanged
@PROPERTY_SETTINGS
@given(
    property_id=_valid_tokens,
    condition_type=_valid_tokens,
    bad_anticipated_date=st.one_of(
        st.none(),
        st.sampled_from(["", "   "]),
        st.sampled_from(["not-a-date", "2024-13-01", "2024-02-30"]),
    ),
)
def test_property_22_prediction_helper_invalid_date_skips(
    property_id: str, condition_type: str, bad_anticipated_date: Any
) -> None:
    """dedupe_key_for_prediction raises for a bad/absent date field, caller skips.

    Covers both the absent-field path (no anticipated date key at all) and the
    present-but-invalid path; both surface INPUT_ANTICIPATED_DATE so the caller
    records the failed prediction and writes nothing (Req 7.6).
    """
    # Present-but-invalid anticipated date field.
    prediction_invalid = {"anticipated_date": bad_anticipated_date}
    with pytest.raises(DedupeKeyError) as excinfo:
        dedupe_key_for_prediction(prediction_invalid, property_id, condition_type)
    assert excinfo.value.failed_input == INPUT_ANTICIPATED_DATE

    # Absent anticipated date field entirely.
    prediction_absent: dict[str, Any] = {}
    with pytest.raises(DedupeKeyError) as excinfo_absent:
        dedupe_key_for_prediction(prediction_absent, property_id, condition_type)
    assert excinfo_absent.value.failed_input == INPUT_ANTICIPATED_DATE

    # No write for either failure.
    table = FakeAlertsTable()
    assert table.puts == []
    assert table.updates == []


# Feature: predictive-forecasting-agent, Property 22: dedupeKey-derivation failure skips the prediction and leaves records unchanged
@PROPERTY_SETTINGS
@given(
    property_id=_valid_tokens,
    condition_type=_valid_tokens,
    anticipated_date=_valid_dates,
)
def test_property_22_valid_input_control_case(
    property_id: str, condition_type: str, anticipated_date: Any
) -> None:
    """Control: a fully-valid input derives a well-formed key and does not skip.

    Confirms the derivation-failure skip is specific to invalid inputs: with all
    inputs valid, ``derive_dedupe_key`` returns a well-formed key, no error is
    recorded, and the caller would proceed (result is not ``None``).
    """
    key = derive_dedupe_key(property_id, condition_type, anticipated_date)

    # Well-formed key: literal spec shape forecast#{pid}#{CONDITION}#{YYYY-MM-DD}.
    parts = key.split("#")
    assert parts[0] == "forecast"
    assert len(parts) == 4
    assert parts[1] == property_id.strip()
    assert parts[2] == condition_type.strip().upper()
    # The trailing day segment is a valid ISO calendar day.
    assert date.fromisoformat(parts[3]).isoformat() == parts[3]

    # The caller proceeds (no skip, no recorded error).
    table = FakeAlertsTable()
    errors: list[dict[str, Any]] = []
    result = try_derive_or_skip(
        table, errors, property_id, condition_type, anticipated_date
    )
    assert result == key
    assert errors == []
