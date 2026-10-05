"""Property test for the pure Forecast Engine oversell classification.

Covers the oversell-classification step of the deterministic Forecast Engine
(``forecast.engine.classify_oversells``): for every evaluable horizon date, the
date is classified as an Oversell_Condition exactly when forecasted demand
(occupied + arriving rooms) exceeds sellable capacity by 1 or more rooms, and
the reported ``rooms_oversold`` is fixed to that demand-minus-capacity
shortfall. Because the engine is pure (no I/O, no clients, no model), this is
exercised directly with Hypothesis over generated per-date demand inputs, all
with ``has_sufficient_data=True`` so every generated date is evaluable and the
oversell decision is the only thing under test.

Validates: Requirements 3.1, 3.2.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import DailyDemand, OversellPrediction, classify_oversells

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# Every generated date is evaluable so the oversell decision is isolated; the
# min_data_points bar is set to the floor so sufficiency never excludes a date.
MIN_DATA_POINTS = 0


def _daily_demand(day_index: int) -> st.SearchStrategy[DailyDemand]:
    """Build a Hypothesis strategy for one evaluable ``DailyDemand``.

    Dates are made distinct and valid by offsetting a fixed base date by
    ``day_index`` days, so a tuple of these carries unique ISO dates. Room
    counts span both sides of the oversell boundary so oversold and
    non-oversold dates are both generated. ``has_sufficient_data`` is always
    ``True`` so the date is evaluable and only the oversell rule is tested.

    Args:
        day_index: The offset in days from the base date, giving each generated
            demand in a sequence a distinct calendar date.

    Returns:
        A strategy producing a single evaluable ``DailyDemand``.
    """
    iso_date = (date(2025, 1, 1) + timedelta(days=day_index)).isoformat()
    return st.builds(
        DailyDemand,
        date=st.just(iso_date),
        occupied_rooms=st.integers(min_value=0, max_value=500),
        arriving_rooms=st.integers(min_value=0, max_value=500),
        sellable_capacity=st.integers(min_value=0, max_value=500),
        has_sufficient_data=st.just(True),
    )


# A horizon of 1..14 distinct-dated, evaluable daily demands.
DAILY_DEMANDS = st.integers(min_value=1, max_value=14).flatmap(
    lambda n: st.tuples(*[_daily_demand(i) for i in range(n)])
)

PROPERTY_IDS = st.sampled_from(
    ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001"]
)


# ---------------------------------------------------------------------------
# Property 9: Oversell classification and rooms-oversold
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 9: Oversell classification and rooms-oversold
@PROPERTY_SETTINGS
@given(daily_demands=DAILY_DEMANDS, property_id=PROPERTY_IDS)
def test_property_9_oversell_classification_and_rooms_oversold(
    daily_demands: tuple[DailyDemand, ...], property_id: str
) -> None:
    """A date is an OVERSELL prediction iff demand - capacity >= 1.

    Asserts, for any tuple of evaluable per-date demand inputs:
    - a date produces an OVERSELL prediction exactly when
      occupied + arriving - sellable_capacity >= 1;
    - every classified prediction reports
      ``rooms_oversold == occupied + arriving - sellable_capacity`` and
      ``rooms_oversold >= 1``;
    - non-oversold evaluable dates produce no prediction;
    - every prediction carries ``condition_type == "OVERSELL"`` and the bound
      ``property_id``.
    """
    predictions, unevaluable_dates = classify_oversells(
        daily_demands, property_id, MIN_DATA_POINTS
    )

    # Every generated date is evaluable, so nothing is excluded for no-data.
    assert unevaluable_dates == ()

    predictions_by_date = {p.anticipated_date: p for p in predictions}

    for demand in daily_demands:
        expected_oversold = (
            demand.occupied_rooms
            + demand.arriving_rooms
            - demand.sellable_capacity
        )
        is_oversold = expected_oversold >= 1

        if is_oversold:
            # The date must be classified, with the deterministic shortfall.
            assert demand.date in predictions_by_date
            prediction = predictions_by_date[demand.date]
            assert isinstance(prediction, OversellPrediction)
            assert prediction.rooms_oversold == expected_oversold
            assert prediction.rooms_oversold >= 1
            assert prediction.condition_type == "OVERSELL"
            assert prediction.property_id == property_id
        else:
            # Non-oversold evaluable dates produce no prediction.
            assert demand.date not in predictions_by_date
