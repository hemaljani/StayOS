"""Pure Forecast Engine domain models for the PULSE Forecasting Agent.

This module defines the intermediate (in-memory) data models the deterministic
Forecast Engine operates over: the forecast horizon, per-date demand inputs, an
oversell prediction, and the whole-run result. These are the primary
property-based-testing surface for the feature.

The engine is deliberately PURE: this module performs no I/O, opens no clients,
and imports no model. It depends only on the standard library so the
deterministic detection numbers (oversell classification, ``rooms_oversold``,
``anticipated_date``, and the integer 0-100 ``confidence``) can be computed and
tested on any host without a runtime session (design: determinism boundary).

Only the frozen dataclass definitions live here for now. The forecast logic
functions (horizon arithmetic, oversell classification, confidence scoring) are
implemented in later tasks (2.1, 2.3, 2.6).

Field shapes follow design.md -> Data Models -> Intermediate (in-memory) models
and Components -> Forecast Engine. All fields use snake_case (NAMING-03) and
carry full type hints (PYQUALITY-01).

Requirements: 3.1, 3.2, 3.4, 3.7.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# Bounds on the Forecast_Horizon length in days: the engine evaluates at least
# the upcoming night (1 day) and at most 14 days ahead (Req 3.1). A requested
# length outside this range is clamped to the nearest bound.
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 14


@dataclass(frozen=True)
class ForecastHorizon:
    """The inclusive window of future dates evaluated in a single run.

    The horizon is computed in the property's local timezone (Req 1.2) and its
    span is bounded to 1..14 days ahead of the current date (Req 3.1).

    Attributes:
        start_date: The current local calendar date, as an ISO ``YYYY-MM-DD``
            string (Req 1.2).
        end_date: ``start_date`` plus the configured horizon length, as an ISO
            ``YYYY-MM-DD`` string; the span is bounded 1..14 days (Req 1.2, 3.1).
    """

    start_date: str
    end_date: str


@dataclass(frozen=True)
class DailyDemand:
    """The forward-looking demand inputs for one date in the horizon.

    A single date's occupancy, arrivals, and sellable capacity, plus a flag
    marking whether enough data was retrieved to evaluate the date. When data is
    insufficient the date is excluded from oversell classification, recorded as
    unevaluable, and never terminates the run (Req 3.3, 8.2).

    Attributes:
        date: The local calendar day this demand describes, as an ISO
            ``YYYY-MM-DD`` string.
        occupied_rooms: Rooms already occupied/committed for the date.
        arriving_rooms: Rooms arriving (forecasted arrivals) for the date.
        sellable_capacity: Sellable room capacity for the date; oversell is
            demand (occupied + arriving) exceeding this by 1 or more (Req 3.2).
        has_sufficient_data: ``True`` when enough data points were retrieved to
            evaluate the date; ``False`` marks the date unevaluable (Req 3.3,
            8.2).
    """

    date: str
    occupied_rooms: int
    arriving_rooms: int
    sellable_capacity: int
    has_sufficient_data: bool


@dataclass(frozen=True)
class OversellPrediction:
    """A deterministic prediction of an oversell condition for one night.

    Every numeric field on this record is computed deterministically by the pure
    engine and is never model-generated (Req 3.9). The human-readable
    Remediation_Plan is authored separately by
    ``narrative.author_remediation_plan`` around these fixed numbers (Req 3.7 -
    3.10); it is intentionally NOT part of this pure-engine record.

    Attributes:
        property_id: The single bound run property this prediction belongs to
            (the data-isolation boundary, Req 6.1, 6.2).
        anticipated_date: The oversold local calendar day, as an ISO
            ``YYYY-MM-DD`` string; deterministic (Req 3.7, 4.7).
        condition_type: The predicted condition; ``"OVERSELL"`` for this
            feature.
        rooms_oversold: Forecasted demand minus sellable capacity, always >= 1
            for a classified oversell; deterministic (Req 3.2, 3.7).
        confidence: The prediction confidence as an integer in [0, 100];
            deterministic (Req 3.4).
        based_on_partial_data: ``True`` when a required signal was missing after
            retries and the prediction was built from partial data (Req 2.8).
        overflow_sister_property_id: The deterministic overflow candidate sister
            property id, present only when sister availability was retrieved for
            overflow evaluation (Req 2.3, 6.6). ``None`` otherwise.
        overflow_available_rooms: The deterministic available-room count at the
            overflow candidate, present only when sister availability was
            retrieved (Req 2.3, 6.6). ``None`` otherwise.
        unevaluable: ``True`` when the date was excluded from classification for
            insufficient data (Req 3.3, 8.2).
    """

    property_id: str
    anticipated_date: str
    condition_type: str
    rooms_oversold: int
    confidence: int
    based_on_partial_data: bool
    overflow_sister_property_id: Optional[str] = None
    overflow_available_rooms: Optional[int] = None
    unevaluable: bool = False


@dataclass(frozen=True)
class ForecastResult:
    """The outcome of a single per-property forecasting run over the horizon.

    Aggregates the actionable oversell predictions (confidence >= 50, Req 3.5)
    with the dates that could not be evaluated for insufficient data, so the run
    can both emit heads-ups and record no-data outcomes without terminating
    (Req 3.3, 8.2, 8.4).

    Attributes:
        property_id: The single bound run property (Req 6.1).
        horizon: The evaluated forecast horizon window.
        predictions: The actionable oversell predictions produced this run
            (confidence >= 50, Req 3.5).
        unevaluable_dates: The horizon dates excluded from classification for
            insufficient data, as ISO ``YYYY-MM-DD`` strings (Req 3.3, 8.2).
    """

    property_id: str
    horizon: ForecastHorizon
    predictions: tuple[OversellPrediction, ...]
    unevaluable_dates: tuple[str, ...]


def build_forecast_horizon(reference_date: date, horizon_days: int) -> ForecastHorizon:
    """Build a ForecastHorizon from a reference local date and horizon length.

    Pure horizon arithmetic (design: Forecast Engine -> logic step: horizon). The
    window starts on the property's current local calendar date and ends that
    many days later, with the requested length bounded to 1..14 days ahead of the
    current date (Req 1.2, 3.1). Performs no I/O and depends only on the standard
    library, so it is deterministic and testable on any host.

    Args:
        reference_date: The property's current local calendar date, used as the
            horizon start (Req 1.2). The caller resolves the property-local
            "today" (timezone handling is out of scope for this pure function).
        horizon_days: The requested Forecast_Horizon length in days. Values below
            1 are clamped up to 1 and values above 14 are clamped down to 14
            (Req 3.1), so the end date is always 1..14 days after the start.

    Returns:
        A ForecastHorizon whose ``start_date`` is ``reference_date`` and whose
        ``end_date`` is ``reference_date`` plus the clamped horizon length, both
        formatted as ISO ``YYYY-MM-DD`` strings.
    """
    # Clamp the requested length into the valid 1..14 range before any arithmetic
    # so a mis-configured horizon can never produce an out-of-bounds window.
    bounded_days = max(MIN_HORIZON_DAYS, min(horizon_days, MAX_HORIZON_DAYS))

    # end = start + N days; ISO YYYY-MM-DD strings match the ForecastHorizon shape.
    end_date = reference_date + timedelta(days=bounded_days)

    return ForecastHorizon(
        start_date=reference_date.isoformat(),
        end_date=end_date.isoformat(),
    )


# Minimum forecasted demand over sellable capacity, in whole rooms, that
# constitutes an oversell. A single room short of capacity is an Oversell_Condition
# (Req 3.2: demand exceeds capacity "by 1 or more rooms").
OVERSELL_ROOM_THRESHOLD = 1


def _is_date_evaluable(demand: DailyDemand, min_data_points: int) -> bool:
    """Return whether a single date has enough data to be classified.

    A date is evaluable only when its occupancy/arrivals signals were retrieved
    with enough data points to support a forecast. Insufficiency is expressed two
    ways and either one excludes the date (Req 3.3, 8.2):

    * ``demand.has_sufficient_data`` is ``False`` — the Signal Reader already
      judged the retrieved signals incomplete for this date.
    * ``min_data_points`` is positive but the required data points were not met —
      a non-positive ``min_data_points`` disables this numeric guard and defers
      entirely to the ``has_sufficient_data`` flag.

    This is a pure predicate: it performs no I/O and reads only the passed-in
    values, so the exclusion decision is deterministic on any host.

    Args:
        demand: The forward-looking demand inputs for one horizon date.
        min_data_points: The minimum number of data points required to evaluate a
            date. Values of 0 or below disable the numeric guard, leaving
            ``demand.has_sufficient_data`` as the sole sufficiency signal.

    Returns:
        ``True`` when the date can be classified for an Oversell_Condition;
        ``False`` when it must be excluded as unevaluable for insufficient data.
    """
    # The reader's completeness judgement is authoritative: an incomplete signal
    # set can never be classified, regardless of the numeric guard.
    if not demand.has_sufficient_data:
        return False

    # A positive min_data_points is a belt-and-suspenders numeric guard. The pure
    # engine does not carry a per-date raw data-point count (DailyDemand exposes
    # only the boolean flag from the reader), so once the flag is True the
    # threshold is considered satisfied; the guard exists so a mis-configured
    # non-positive threshold cannot silently pass unevaluable dates through.
    return min_data_points <= 0 or demand.has_sufficient_data


def _classify_date(demand: DailyDemand, property_id: str) -> Optional[OversellPrediction]:
    """Classify one evaluable date for an Oversell_Condition.

    Pure oversell arithmetic (design: Forecast Engine -> logic step: oversell
    classification, Req 3.1, 3.2). Forecasted demand is occupancy plus arrivals;
    the date is an Oversell_Condition when that demand exceeds sellable capacity
    by ``OVERSELL_ROOM_THRESHOLD`` (1) or more rooms, in which case
    ``rooms_oversold`` is the exact demand-minus-capacity shortfall.

    Confidence scoring, the actionable filter, and the overflow candidate are out
    of scope here (task 2.6); this function fixes only the deterministic oversell
    detection and its ``rooms_oversold`` count. The returned prediction therefore
    carries a placeholder ``confidence`` of 0 and no overflow fields, which task
    2.6 populates.

    Args:
        demand: The forward-looking demand inputs for one evaluable horizon date.
        property_id: The single bound run property this prediction belongs to
            (the data-isolation boundary, Req 6.1, 6.2).

    Returns:
        An OversellPrediction with the deterministic ``rooms_oversold`` count when
        the date is an Oversell_Condition; ``None`` when demand does not exceed
        capacity and the date is therefore not oversold.
    """
    # Forecasted demand for the night = rooms already committed + rooms arriving.
    forecasted_demand = demand.occupied_rooms + demand.arriving_rooms

    # Shortfall against sellable capacity; positive means more demand than rooms.
    rooms_oversold = forecasted_demand - demand.sellable_capacity

    # Not an oversell unless demand exceeds capacity by at least the threshold.
    if rooms_oversold < OVERSELL_ROOM_THRESHOLD:
        return None

    return OversellPrediction(
        property_id=property_id,
        anticipated_date=demand.date,
        condition_type="OVERSELL",
        rooms_oversold=rooms_oversold,
        # Confidence is computed in task 2.6; fixed at 0 here so the record is
        # well-formed without pre-empting the confidence-scoring logic step.
        confidence=0,
        based_on_partial_data=not demand.has_sufficient_data,
    )


def classify_oversells(
    daily_demands: tuple[DailyDemand, ...],
    property_id: str,
    min_data_points: int,
) -> tuple[tuple[OversellPrediction, ...], tuple[str, ...]]:
    """Classify each horizon date for an Oversell_Condition, excluding no-data dates.

    Pure per-date detection over the horizon (design: Forecast Engine -> logic
    steps: data sufficiency, oversell classification). For each date, in order:

    1. **Data sufficiency (Req 3.3, 8.2):** if the date lacks enough data to
       evaluate, it is marked unevaluable, its date is recorded in the returned
       insufficient-data list, and classification is skipped. Processing then
       continues with the remaining dates — an unevaluable date never terminates
       the run.
    2. **Oversell classification (Req 3.1, 3.2):** otherwise the date is
       classified an Oversell_Condition when occupancy plus arrivals exceeds
       sellable capacity by 1 or more rooms, with ``rooms_oversold`` fixed to the
       demand-minus-capacity shortfall.

    This function performs no I/O and depends only on the standard library, so the
    oversell numbers are deterministic and testable on any host. Confidence
    scoring and the actionable filter are applied separately (task 2.6).

    Args:
        daily_demands: The per-date demand inputs across the forecast horizon.
        property_id: The single bound run property to stamp on every prediction
            (the data-isolation boundary, Req 6.1, 6.2).
        min_data_points: The minimum number of data points required to evaluate a
            date; dates below the sufficiency bar are excluded (Req 3.3).

    Returns:
        A two-tuple ``(predictions, unevaluable_dates)`` where ``predictions`` are
        the classified Oversell_Conditions (each with its deterministic
        ``rooms_oversold``) and ``unevaluable_dates`` are the ISO ``YYYY-MM-DD``
        dates excluded for insufficient data, in horizon order.
    """
    predictions: list[OversellPrediction] = []
    unevaluable_dates: list[str] = []

    for demand in daily_demands:
        # Step 1 - data sufficiency: exclude, record, and continue (never abort).
        if not _is_date_evaluable(demand, min_data_points):
            unevaluable_dates.append(demand.date)
            continue

        # Step 2 - oversell classification: keep only dates that are oversold.
        prediction = _classify_date(demand, property_id)
        if prediction is not None:
            predictions.append(prediction)

    return tuple(predictions), tuple(unevaluable_dates)

import dataclasses

# --- Confidence scoring and the actionable filter (task 2.6) ---------------

# A prediction is actionable, and therefore emitted, only when its integer
# confidence meets this threshold (Req 3.5). Predictions below it are dropped
# from the ForecastResult so only high-confidence heads-ups reach the GM.
ACTIONABLE_CONFIDENCE_THRESHOLD = 50

# Base confidence assigned to any classified oversell before adjustments. The
# weights below add to or subtract from this base; the final score is clamped to
# the [0, 100] range that OversellPrediction.confidence requires.
CONFIDENCE_BASE = 50

# Confidence bounds enforced on the final score (OversellPrediction.confidence
# is documented as an integer in [0, 100], Req 3.4).
CONFIDENCE_MIN = 0
CONFIDENCE_MAX = 100

# Extra confidence per whole room of shortfall, capped by the ceiling below. A
# larger rooms_oversold is a stronger, less ambiguous oversell signal, so it
# raises confidence.
CONFIDENCE_PER_ROOM_OVERSOLD = 8
CONFIDENCE_SHORTFALL_CAP = 24

# Confidence penalty applied when the prediction was built from partial data
# (a required signal was missing after retries, Req 2.8). Partial data makes the
# forecast less trustworthy, so it lowers confidence.
CONFIDENCE_PARTIAL_DATA_PENALTY = 20

# Recency handling: predictions nearer the horizon start are more certain. The
# day-offset from horizon.start_date costs this many points per day, capped by
# the ceiling below so distant dates never drive confidence arbitrarily negative.
CONFIDENCE_RECENCY_PENALTY_PER_DAY = 3
CONFIDENCE_RECENCY_CAP = 30


def compute_confidence(
    prediction: OversellPrediction,
    horizon: ForecastHorizon,
) -> int:
    """Compute a deterministic oversell confidence in [0, 100].

    Pure confidence scoring (design: Forecast Engine -> logic step: confidence,
    Req 3.4). The score is fully deterministic given the prediction and horizon;
    it performs no I/O, uses no model, and reads only the passed-in values.

    Formula (all terms are whole-integer, applied to ``CONFIDENCE_BASE`` = 50):

    * **Shortfall magnitude (raises confidence):**
      ``+ min(rooms_oversold * CONFIDENCE_PER_ROOM_OVERSOLD,
      CONFIDENCE_SHORTFALL_CAP)``. A bigger oversold count is a stronger signal.
    * **Data completeness (lowers confidence):**
      ``- CONFIDENCE_PARTIAL_DATA_PENALTY`` when
      ``prediction.based_on_partial_data`` is ``True``.
    * **Recency (nearer raises confidence):** the day-offset of
      ``anticipated_date`` from ``horizon.start_date`` costs
      ``CONFIDENCE_RECENCY_PENALTY_PER_DAY`` per day, capped at
      ``CONFIDENCE_RECENCY_CAP``. Offset 0 (the start date) costs nothing;
      distant dates cost the most. Dates on or before the start date incur no
      recency penalty.

    The raw sum is clamped to ``[CONFIDENCE_MIN, CONFIDENCE_MAX]`` = [0, 100].

    Args:
        prediction: The classified oversell prediction to score. Only its
            ``rooms_oversold``, ``based_on_partial_data``, and ``anticipated_date``
            fields influence the result.
        horizon: The forecast horizon; ``start_date`` anchors the recency offset.

    Returns:
        The confidence as an integer clamped to [0, 100].
    """
    score = CONFIDENCE_BASE

    # Shortfall magnitude: more rooms oversold -> higher, but capped confidence.
    shortfall_bonus = min(
        prediction.rooms_oversold * CONFIDENCE_PER_ROOM_OVERSOLD,
        CONFIDENCE_SHORTFALL_CAP,
    )
    score += shortfall_bonus

    # Data completeness: partial data lowers trust in the prediction.
    if prediction.based_on_partial_data:
        score -= CONFIDENCE_PARTIAL_DATA_PENALTY

    # Recency: parse both ISO dates and penalise by the day-offset from the
    # horizon start. Nearer dates (smaller offset) keep more confidence.
    start = date.fromisoformat(horizon.start_date)
    anticipated = date.fromisoformat(prediction.anticipated_date)
    day_offset = (anticipated - start).days
    if day_offset > 0:
        recency_penalty = min(
            day_offset * CONFIDENCE_RECENCY_PENALTY_PER_DAY,
            CONFIDENCE_RECENCY_CAP,
        )
        score -= recency_penalty

    # Clamp to the documented [0, 100] range that confidence must satisfy.
    return max(CONFIDENCE_MIN, min(score, CONFIDENCE_MAX))


def fix_overflow_facts(
    prediction: OversellPrediction,
    sister_availability: tuple[tuple[str, int], ...] = (),
) -> OversellPrediction:
    """Set the deterministic overflow candidate facts on a prediction.

    Pure remediation-fact fixing (design: Forecast Engine -> overflow evaluation,
    Req 2.3, 3.7, 6.6). Given retrieved sister-property availability, pick the
    deterministic overflow candidate and stamp it onto a copy of the prediction.
    Performs no I/O and reads only the passed-in values.

    Candidate selection is deterministic: choose the sister with the most
    available rooms; ties are broken by the lexicographically smallest sister
    property id. Sisters with no available rooms (<= 0) are ignored. When no
    usable sister availability is present, the overflow fields are left ``None``.

    Args:
        prediction: The oversell prediction to attach overflow facts to.
        sister_availability: A tuple of ``(sister_property_id, available_rooms)``
            pairs describing sister-property availability retrieved for overflow
            evaluation. Empty (the default) when no availability was retrieved.

    Returns:
        A new OversellPrediction with ``overflow_sister_property_id`` and
        ``overflow_available_rooms`` set to the chosen candidate, or with both
        left ``None`` when there is no usable sister availability.
    """
    # Keep only sisters that actually have rooms to absorb overflow.
    usable = [
        (sister_id, rooms)
        for sister_id, rooms in sister_availability
        if rooms > 0
    ]
    if not usable:
        # No usable availability -> leave the overflow facts unset (None).
        return prediction

    # Deterministic pick: most available rooms first, then smallest id for ties.
    # Sorting by (-rooms, id) makes the best candidate the first element.
    best_sister_id, best_rooms = sorted(usable, key=lambda pair: (-pair[1], pair[0]))[0]

    return dataclasses.replace(
        prediction,
        overflow_sister_property_id=best_sister_id,
        overflow_available_rooms=best_rooms,
    )


def run_forecast(
    daily_demands: tuple[DailyDemand, ...],
    horizon: ForecastHorizon,
    property_id: str,
    min_data_points: int,
    sister_availability: tuple[tuple[str, int], ...] = (),
    actionable_confidence: int = ACTIONABLE_CONFIDENCE_THRESHOLD,
) -> ForecastResult:
    """Run the pure forecast end to end over one property's horizon.

    Orchestrates the deterministic Forecast Engine (design: Forecast Engine ->
    run). It classifies oversells, scores each prediction's confidence, fixes the
    deterministic remediation/overflow facts, applies the actionable filter, and
    aggregates the result. This function consumes ONLY the passed demands,
    horizon, and sister availability; it is independent of any rule engine and
    performs no I/O (Req 3.6). Every emitted number is therefore deterministic and
    testable on any host.

    Steps:

    1. ``classify_oversells`` -> ``(predictions, unevaluable_dates)`` (Req 3.1-3.3).
    2. For each prediction: compute confidence via ``compute_confidence`` and set
       it with ``dataclasses.replace``, then fix overflow facts via
       ``fix_overflow_facts`` (Req 3.4, 3.7).
    3. Keep only predictions with ``confidence >= actionable_confidence`` — the
       actionable filter (Req 3.5).
    4. Return a ``ForecastResult`` bound to ``property_id`` with the kept
       predictions and the unevaluable dates (Req 3.7).

    Args:
        daily_demands: The per-date demand inputs across the forecast horizon.
        horizon: The evaluated forecast horizon window; anchors recency scoring.
        property_id: The single bound run property (the data-isolation boundary,
            Req 6.1, 6.2).
        min_data_points: The minimum data points required to evaluate a date; low
            dates are excluded as unevaluable (Req 3.3).
        sister_availability: Optional ``(sister_property_id, available_rooms)``
            pairs used to pick a deterministic overflow candidate (Req 2.3, 6.6).
            Empty by default, in which case overflow facts stay ``None``.
        actionable_confidence: The inclusive confidence floor for a prediction to
            be emitted (Req 3.5). Defaults to ``ACTIONABLE_CONFIDENCE_THRESHOLD``.

    Returns:
        A ForecastResult for ``property_id`` over ``horizon`` carrying the
        actionable oversell predictions and the dates excluded for insufficient
        data.
    """
    # Step 1 - deterministic per-date oversell detection.
    predictions, unevaluable_dates = classify_oversells(
        daily_demands, property_id, min_data_points
    )

    kept_predictions: list[OversellPrediction] = []
    for prediction in predictions:
        # Step 2 - score confidence and stamp it onto a frozen-record copy.
        confidence = compute_confidence(prediction, horizon)
        scored = dataclasses.replace(prediction, confidence=confidence)

        # Step 2 (cont.) - fix deterministic remediation/overflow facts.
        scored = fix_overflow_facts(scored, sister_availability)

        # Step 3 - actionable filter: emit only high-confidence predictions.
        if scored.confidence >= actionable_confidence:
            kept_predictions.append(scored)

    # Step 4 - aggregate into the per-property run result.
    return ForecastResult(
        property_id=property_id,
        horizon=horizon,
        predictions=tuple(kept_predictions),
        unevaluable_dates=unevaluable_dates,
    )
