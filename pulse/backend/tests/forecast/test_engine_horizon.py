"""Property test for the pure Forecast Engine horizon date arithmetic.

Covers the horizon-building step of the deterministic Forecast Engine
(``forecast.engine.build_forecast_horizon``): the window starts on the
property's current local calendar date and ends that date plus the configured
horizon length, with the requested length clamped to the 1..14 day bounds
(``MIN_HORIZON_DAYS``..``MAX_HORIZON_DAYS``). Because the engine is pure (no
I/O, no clients, no model), this is exercised directly with Hypothesis over a
reference date and an integer horizon length spanning below / within / above the
bounds.

Validates: Requirements 1.2, 3.1.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import (
    MAX_HORIZON_DAYS,
    MIN_HORIZON_DAYS,
    ForecastHorizon,
    build_forecast_horizon,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)


def _clamp(value: int, low: int, high: int) -> int:
    """Return ``value`` clamped into the inclusive ``[low, high]`` range."""
    return max(low, min(value, high))


# ---------------------------------------------------------------------------
# Property 1: Forecast horizon date arithmetic
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 1: Forecast horizon date arithmetic
@PROPERTY_SETTINGS
@given(
    reference_date=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)),
    # Span below (< 1), within (1..14), and above (> 14) the horizon bounds so
    # the clamp behavior on both sides is exercised.
    horizon_days=st.integers(min_value=-30, max_value=60),
)
def test_property_1_forecast_horizon_date_arithmetic(
    reference_date: date, horizon_days: int
) -> None:
    """Horizon arithmetic: start == reference; span == clamped length in 1..14.

    Asserts, for any reference local date and any requested horizon length:
    - ``start_date`` equals ``reference_date`` in ISO ``YYYY-MM-DD`` form;
    - the day-span between ``end_date`` and ``start_date`` equals the CLAMPED
      horizon length ``clamp(N, 1, 14)``;
    - ``end_date`` is strictly after ``start_date`` (span >= 1) and at most 14
      days after (span <= 14);
    - both output dates are valid ISO ``YYYY-MM-DD`` strings.
    """
    horizon = build_forecast_horizon(reference_date, horizon_days)

    # The engine returns a ForecastHorizon of ISO strings.
    assert isinstance(horizon, ForecastHorizon)

    # Output dates are valid ISO YYYY-MM-DD: round-tripping through fromisoformat
    # both proves validity and gives us date objects to measure the span.
    parsed_start = date.fromisoformat(horizon.start_date)
    parsed_end = date.fromisoformat(horizon.end_date)
    assert horizon.start_date == parsed_start.isoformat()
    assert horizon.end_date == parsed_end.isoformat()

    # start_date equals the reference date.
    assert horizon.start_date == reference_date.isoformat()
    assert parsed_start == reference_date

    # The day-span equals the clamped horizon length clamp(N, 1, 14).
    expected_span = _clamp(horizon_days, MIN_HORIZON_DAYS, MAX_HORIZON_DAYS)
    span = (parsed_end - parsed_start).days
    assert span == expected_span

    # end_date is strictly after start_date and at most 14 days after.
    assert span >= MIN_HORIZON_DAYS
    assert span <= MAX_HORIZON_DAYS
    assert parsed_end > parsed_start
