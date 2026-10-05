"""Property test: every operational read is scoped to the single bound property.

Covers the Signal Reader's property-scoping contract
(``forecast.signals.read_signals``): every READ-ONLY Gateway tool call the
reader makes for a run MUST carry the single ``propertyId`` the run is bound to
(``PROPERTY_ID_ARG``), and no call may carry any other property id (the
data-isolation boundary, Req 2.4, 6.1). This is exercised with Hypothesis over a
bound property id sampled from the enabled fleet and the ``needs_overflow``
boolean (which decides whether the sister-availability read happens too), driving
``read_signals`` through the :class:`RecordingToolCaller` seam with injected
no-op timeout/sleep seams so no real threads or wall-clock delays are involved.

Validates: Requirements 2.4, 6.1.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import build_forecast_horizon
from forecast.signals import read_signals

from .conftest import RecordingToolCaller

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The enabled fleet property ids a run may be bound to (mirrors the
# FORECAST_ENABLED_PROPERTY_IDS the conftest env fixture sets). Any one of these
# is a valid single bound propertyId for a forecasting run.
BOUND_PROPERTY_IDS: tuple[str, ...] = (
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)


def _canned_results(property_id: str) -> dict[str, object]:
    """Build canned tool results whose records belong to the bound property.

    Returns:
        A mapping of bare tool name -> canned result for the three READ-ONLY
        tools, each nesting a single record carrying ``property_id`` so the
        property-isolation filter retains it and no zero-match noise arises.
    """
    return {
        "get_occupancy": {"records": [{"propertyId": property_id, "occupancy": 80}]},
        "get_revenue": {"records": [{"propertyId": property_id, "revenue": 12000}]},
        "get_sister_property_availability": {
            "records": [{"propertyId": "ALOHA-OTHER-999", "availableRooms": 5}]
        },
    }


# Feature: predictive-forecasting-agent, Property 3: Every operational read is scoped to the single bound propertyId
@PROPERTY_SETTINGS
@given(
    property_id=st.sampled_from(BOUND_PROPERTY_IDS),
    needs_overflow=st.booleans(),
)
def test_property_3_every_read_scoped_to_bound_property(
    property_id: str, needs_overflow: bool
) -> None:
    """Every recorded tool call carries the one bound propertyId, no other.

    Asserts, for any bound property sampled from the enabled fleet and either
    overflow mode:
    - at least one tool call was made (the reader is not a no-op);
    - EVERY recorded call carried ``propertyId`` equal to the bound
      ``property_id`` (Req 2.4, 6.1);
    - NO recorded call carried a different (or missing) ``propertyId``.
    """
    caller = RecordingToolCaller(_canned_results(property_id))
    horizon = build_forecast_horizon(date(2025, 6, 1), 14)

    # Inject no-op timeout/sleep seams so no real threads or wall-clock delays
    # occur; the run stays deterministic and fast.
    read_signals(
        property_id,
        horizon,
        caller,
        needs_overflow=needs_overflow,
        timeout_runner=lambda thunk, _timeout: thunk(),
        sleep=lambda _seconds: None,
    )

    recorded_property_ids = caller.property_ids()

    # The reader must have actually read something (occupancy + revenue always).
    assert recorded_property_ids, "expected read_signals to make at least one tool call"

    # Every call is scoped to the single bound property (Req 2.4, 6.1) ...
    assert all(
        recorded == property_id for recorded in recorded_property_ids
    ), f"a tool call was not scoped to the bound property: {recorded_property_ids}"

    # ... and no call carried a different propertyId.
    assert not any(
        recorded != property_id for recorded in recorded_property_ids
    ), f"a tool call carried a foreign propertyId: {recorded_property_ids}"
