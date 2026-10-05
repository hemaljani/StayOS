"""Property test for the Signal Reader's conditional sister-availability read.

Covers the two-phase read contract of ``forecast.signals.read_signals`` (design:
Components -> ``signals.SignalReader``): occupancy and revenue are ALWAYS read,
while sister-property availability is read ONLY when overflow evaluation is
needed (Req 2.3). Because the reader talks to the Gateway exclusively through the
injected ``ToolCaller`` seam, this is exercised with a recording fake caller and
deterministic (no-op) timeout / backoff seams, so no client, clock, or network is
involved. Hypothesis generates the ``needs_overflow`` boolean so both branches of
the second-phase decision are covered.

Validates: Requirements 2.3.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import build_forecast_horizon
from forecast.signals import (
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
    read_signals,
)

from .conftest import RecordingToolCaller

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The single bound run property every read is scoped to (Req 6.1). Its records
# match the bound id so the property-isolation filter keeps them.
RUN_PROPERTY_ID = "ALOHA-CHI-001"

# Canned results for every read-only tool the reader may invoke. Each record
# carries the bound ``propertyId`` so it survives the isolation filter and the
# reader treats the read as a successful signal (not a degraded one).
CANNED_RESULTS = {
    TOOL_GET_OCCUPANCY: [{"propertyId": RUN_PROPERTY_ID, "occupancy": 42}],
    TOOL_GET_REVENUE: [{"propertyId": RUN_PROPERTY_ID, "revenue": 1000}],
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY: [
        {"propertyId": "ALOHA-MIA-001", "availableRooms": 5}
    ],
}


# ---------------------------------------------------------------------------
# Property 5: conditional sister-availability read
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 5: Sister-property availability is fetched only when overflow evaluation is needed
@PROPERTY_SETTINGS
@given(needs_overflow=st.booleans())
def test_property_5_conditional_sister_availability(needs_overflow: bool) -> None:
    """Sister availability is read iff overflow evaluation is needed (Req 2.3).

    Asserts, for either value of ``needs_overflow``:
    - occupancy and revenue are ALWAYS read (Req 2.1, 2.2);
    - when ``needs_overflow`` is ``False``, ``get_sister_property_availability``
      is NEVER called (the second phase is skipped, Req 2.3);
    - when ``needs_overflow`` is ``True``, ``get_sister_property_availability`` is
      called EXACTLY once, in addition to occupancy and revenue (Req 2.3).

    Deterministic timeout / backoff seams are injected so the reader runs without
    any real clock, thread, or wall-clock delay.
    """
    call_tool = RecordingToolCaller(CANNED_RESULTS)
    horizon = build_forecast_horizon(date(2025, 1, 1), 14)

    read_signals(
        RUN_PROPERTY_ID,
        horizon,
        call_tool,
        needs_overflow=needs_overflow,
        # Run each thunk inline (no worker thread / real timeout) and never sleep,
        # so the property runs deterministically without wall-clock delay.
        timeout_runner=lambda thunk, _timeout: thunk(),
        sleep=lambda _seconds: None,
    )

    tool_names = call_tool.tool_names()
    sister_calls = tool_names.count(TOOL_GET_SISTER_PROPERTY_AVAILABILITY)

    # Phase 1 always runs: occupancy and revenue are read regardless of overflow.
    assert TOOL_GET_OCCUPANCY in tool_names
    assert TOOL_GET_REVENUE in tool_names

    if needs_overflow:
        # Phase 2 runs exactly once when overflow evaluation is needed (Req 2.3).
        assert sister_calls == 1
    else:
        # Phase 2 is skipped entirely when overflow is not needed (Req 2.3).
        assert sister_calls == 0
