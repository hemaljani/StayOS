"""Property test for single-tool total failure graceful degradation in the reader.

Covers the graceful-degradation contract of the Signal Reader
(``forecast.signals.read_signals``): when one READ-ONLY Gateway tool fails ALL
of its retry attempts, the reader must NOT terminate the run. Instead it leaves
that tool's ``SignalSet`` field ``None``, adds the bare tool name to
``SignalSet.missing_signals``, and RETURNS normally so the run continues, while
every other tool that succeeds has its field populated and stays absent from
``missing_signals`` (Req 2.7, 8.1).

Because the reader takes injectable seams (the ``call_tool`` caller, the timeout
runner, and the backoff sleep), this is exercised directly with Hypothesis over
which tool(s) are designated to fail entirely and whether overflow evaluation
(the sister tool) is requested. The ``timeout_runner`` simply calls the thunk so
the fake ``call_tool`` decides success vs failure, and ``sleep`` is a no-op so no
wall-clock backoff delay is incurred.

Validates: Requirements 2.7, 8.1.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import build_forecast_horizon
from forecast.signals import (
    MAX_ATTEMPTS,
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
    SignalSet,
    read_signals,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The bound run property id every signal is read for (Req 6.1). Canned results
# below carry this id so the property-isolation filter keeps them (not dropped).
RUN_PROPERTY_ID = "ALOHA-CHI-001"

# A distinct sister property id for the sister-availability canned result. It is
# intentionally NOT the bound run id (a property does not overflow to itself).
SISTER_PROPERTY_ID = "ALOHA-MIA-001"

# Canned raw results returned by the fake caller for a tool that SUCCEEDS. Each
# carries the bound run propertyId so the isolation filter retains the record and
# the resulting SignalSet field is populated (non-None).
CANNED_RESULTS = {
    TOOL_GET_OCCUPANCY: [{"propertyId": RUN_PROPERTY_ID, "occupancyRate": 0.82}],
    TOOL_GET_REVENUE: [{"propertyId": RUN_PROPERTY_ID, "revenue": 12500}],
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY: [
        {"propertyId": SISTER_PROPERTY_ID, "availableRooms": 7}
    ],
}


def _no_delay_timeout_runner(thunk, _timeout_seconds):
    """Timeout runner seam that just runs the thunk (no threads, no timing).

    Lets the fake ``call_tool`` decide success vs failure deterministically: if
    the thunk raises, it propagates as a failed attempt exactly as a real timeout
    or tool error would.

    Args:
        thunk: The zero-argument callable performing one tool invocation.
        _timeout_seconds: The (ignored) per-call timeout budget.

    Returns:
        Whatever ``thunk`` returns.
    """
    return thunk()


def _make_failing_caller(failing_tools):
    """Build a fake ``call_tool`` that raises for designated tools, else canned.

    Args:
        failing_tools: The set of bare tool names that must fail EVERY attempt.
            A call to any of these raises, so the reader exhausts its retries and
            degrades that one tool gracefully (Req 2.7).

    Returns:
        A ``call_tool(tool_name, arguments)`` callable returning the canned result
        for a succeeding tool and raising ``RuntimeError`` for a failing one.
    """

    def _call_tool(tool_name, _arguments):
        # A designated failing tool raises on every attempt so all MAX_ATTEMPTS
        # attempts fail and the reader degrades it (field None + missing_signals).
        if tool_name in failing_tools:
            raise RuntimeError(f"tool {tool_name} failed (test)")
        return CANNED_RESULTS[tool_name]

    return _call_tool


# ---------------------------------------------------------------------------
# Property 7: Single-tool total failure degrades gracefully without terminating
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 7: Single-tool total failure degrades gracefully without terminating
@PROPERTY_SETTINGS
@given(
    # Which of the always-read tools fail entirely (occupancy and/or revenue).
    occupancy_fails=st.booleans(),
    revenue_fails=st.booleans(),
    # Whether overflow evaluation is requested (adds the sister tool phase), and
    # whether that sister tool fails entirely when it is read.
    needs_overflow=st.booleans(),
    sister_fails=st.booleans(),
)
def test_property_7_single_tool_failure_degrades_gracefully(
    occupancy_fails: bool,
    revenue_fails: bool,
    needs_overflow: bool,
    sister_fails: bool,
) -> None:
    """A tool failing all attempts degrades to None + missing_signals; run continues.

    Asserts, for any combination of which tools fail entirely:
    - ``read_signals`` RETURNS a ``SignalSet`` (never raises): the run is not
      terminated by a single tool's total failure (Req 2.7, 8.1);
    - each tool that failed all attempts has its ``SignalSet`` field ``None`` and
      its bare name present in ``missing_signals``;
    - each tool that succeeded has its field populated (not ``None``) and its bare
      name absent from ``missing_signals``;
    - the sister tool is only ever read (and thus only degradable) when overflow
      evaluation was requested.
    """
    # Determine the tools that will fail entirely. The sister tool can only fail
    # if it is actually read (overflow requested); otherwise it is never invoked.
    failing_tools = set()
    if occupancy_fails:
        failing_tools.add(TOOL_GET_OCCUPANCY)
    if revenue_fails:
        failing_tools.add(TOOL_GET_REVENUE)
    sister_read_and_fails = needs_overflow and sister_fails
    if sister_read_and_fails:
        failing_tools.add(TOOL_GET_SISTER_PROPERTY_AVAILABILITY)

    call_tool = _make_failing_caller(failing_tools)
    horizon = build_forecast_horizon(date(2025, 6, 1), 7)

    # The run must complete: read_signals returns a SignalSet and does NOT raise,
    # even when one or more tools fail every attempt (graceful degradation).
    signal_set = read_signals(
        RUN_PROPERTY_ID,
        horizon,
        call_tool,
        needs_overflow=needs_overflow,
        # Run each attempt inline (no real threads/timeout) so the fake caller
        # decides success/failure; sleep is a no-op to avoid backoff delay.
        timeout_runner=_no_delay_timeout_runner,
        sleep=lambda _seconds: None,
        max_attempts=MAX_ATTEMPTS,
    )

    assert isinstance(signal_set, SignalSet)

    # Occupancy: failed -> field None and in missing_signals; else populated and
    # absent from missing_signals.
    if occupancy_fails:
        assert signal_set.occupancy is None
        assert TOOL_GET_OCCUPANCY in signal_set.missing_signals
    else:
        assert signal_set.occupancy is not None
        assert TOOL_GET_OCCUPANCY not in signal_set.missing_signals

    # Revenue: same contract as occupancy.
    if revenue_fails:
        assert signal_set.revenue is None
        assert TOOL_GET_REVENUE in signal_set.missing_signals
    else:
        assert signal_set.revenue is not None
        assert TOOL_GET_REVENUE not in signal_set.missing_signals

    # Sister availability is only read when overflow is requested (Req 2.3), so it
    # is only degradable / populated in that case.
    if needs_overflow:
        if sister_fails:
            assert signal_set.sister_availability is None
            assert TOOL_GET_SISTER_PROPERTY_AVAILABILITY in signal_set.missing_signals
        else:
            assert signal_set.sister_availability is not None
            assert (
                TOOL_GET_SISTER_PROPERTY_AVAILABILITY
                not in signal_set.missing_signals
            )
    else:
        # Overflow not requested: the sister tool is never invoked, so its field
        # stays None and it is never recorded as a missing signal.
        assert signal_set.sister_availability is None
        assert TOOL_GET_SISTER_PROPERTY_AVAILABILITY not in signal_set.missing_signals
