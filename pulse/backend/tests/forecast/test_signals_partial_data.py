"""Property test for missing-required-signal partial-data flagging in the reader.

Covers the partial-data flagging contract of the Signal Reader
(``forecast.signals.read_signals``): when a REQUIRED tool (``get_occupancy`` or
``get_revenue``) fails ALL of its retry attempts, the reader leaves that tool's
``SignalSet`` field ``None`` and adds the bare tool name to
``SignalSet.missing_signals``. A downstream consumer derives
``based_on_partial_data`` PRECISELY from whether a required tool is present in
``missing_signals`` (Req 2.8): the prediction is partial-data exactly when
occupancy or revenue is missing.

Because the reader takes injectable seams (the ``call_tool`` caller, the timeout
runner, and the backoff sleep), this is exercised directly with Hypothesis over
which required tools are designated to fail entirely. The ``timeout_runner``
simply calls the thunk so the fake ``call_tool`` decides success vs failure, and
``sleep`` is a no-op so no wall-clock backoff delay is incurred.

Validates: Requirement 2.8.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import build_forecast_horizon
from forecast.signals import (
    MAX_ATTEMPTS,
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
    SignalSet,
    read_signals,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The bound run property id every signal is read for (Req 6.1). Canned results
# below carry this id so the property-isolation filter keeps them (not dropped).
RUN_PROPERTY_ID = "ALOHA-CHI-001"

# Canned raw results returned by the fake caller for a REQUIRED tool that
# SUCCEEDS. Each carries the bound run propertyId so the isolation filter retains
# the record and the resulting SignalSet field is populated (non-None).
CANNED_RESULTS: dict[str, list[dict[str, Any]]] = {
    TOOL_GET_OCCUPANCY: [{"propertyId": RUN_PROPERTY_ID, "occupancyRate": 0.82}],
    TOOL_GET_REVENUE: [{"propertyId": RUN_PROPERTY_ID, "revenue": 12500}],
}


def _no_delay_timeout_runner(thunk: Callable[[], Any], _timeout_seconds: float) -> Any:
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


def _make_failing_caller(failing_tools: set[str]) -> Callable[[str, dict[str, Any]], Any]:
    """Build a fake ``call_tool`` that raises for designated tools, else canned.

    The returned caller also asserts that every invocation carries the bound run
    ``propertyId`` so the canned records it returns are correctly scoped to the
    property under test (Req 2.4, 6.1).

    Args:
        failing_tools: The set of bare tool names that must fail EVERY attempt.
            A call to any of these raises, so the reader exhausts its retries and
            degrades that one tool gracefully (Req 2.7), flagging it in
            ``missing_signals`` (Req 2.8).

    Returns:
        A ``call_tool(tool_name, arguments)`` callable returning the canned result
        for a succeeding tool and raising ``RuntimeError`` for a failing one.
    """

    def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        # Every read must be scoped to the bound property (Req 2.4); the canned
        # record below is only valid because it carries this same propertyId.
        assert arguments.get("propertyId") == RUN_PROPERTY_ID
        # A designated failing tool raises on every attempt so all MAX_ATTEMPTS
        # attempts fail and the reader degrades it (field None + missing_signals).
        if tool_name in failing_tools:
            raise RuntimeError(f"tool {tool_name} failed (test)")
        return CANNED_RESULTS[tool_name]

    return _call_tool


# ---------------------------------------------------------------------------
# Property 8: Missing required signal flags the prediction as partial-data
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 8: Missing required signal flags the prediction as partial-data
@PROPERTY_SETTINGS
@given(
    # Which of the REQUIRED tools fail all attempts (occupancy and/or revenue).
    occupancy_fails=st.booleans(),
    revenue_fails=st.booleans(),
)
def test_property_8_missing_required_signal_flags_partial_data(
    occupancy_fails: bool,
    revenue_fails: bool,
) -> None:
    """A missing required signal (occupancy/revenue) flags partial-data.

    Asserts, for any combination of which required tools fail entirely:
    - when ``get_occupancy`` fails all attempts, ``TOOL_GET_OCCUPANCY`` is in
      ``missing_signals`` and ``SignalSet.occupancy`` is ``None``;
    - when ``get_revenue`` fails all attempts, ``TOOL_GET_REVENUE`` is in
      ``missing_signals`` and ``SignalSet.revenue`` is ``None``;
    - when neither fails, ``missing_signals`` is empty and both fields are
      populated;
    - the DERIVED ``based_on_partial_data`` flag (occupancy or revenue present in
      ``missing_signals``) is ``True`` EXACTLY when a required signal is missing
      (Req 2.8).
    """
    failing_tools: set[str] = set()
    if occupancy_fails:
        failing_tools.add(TOOL_GET_OCCUPANCY)
    if revenue_fails:
        failing_tools.add(TOOL_GET_REVENUE)

    call_tool = _make_failing_caller(failing_tools)
    horizon = build_forecast_horizon(date(2025, 6, 1), 7)

    # Overflow is not requested here: this property is strictly about the two
    # REQUIRED signals and their effect on the derived partial-data flag.
    signal_set = read_signals(
        RUN_PROPERTY_ID,
        horizon,
        call_tool,
        needs_overflow=False,
        # Run each attempt inline (no real threads/timeout) so the fake caller
        # decides success/failure; sleep is a no-op to avoid backoff delay.
        timeout_runner=_no_delay_timeout_runner,
        sleep=lambda _seconds: None,
        max_attempts=MAX_ATTEMPTS,
    )

    assert isinstance(signal_set, SignalSet)

    # Occupancy: failed -> field None and its bare name in missing_signals.
    if occupancy_fails:
        assert TOOL_GET_OCCUPANCY in signal_set.missing_signals
        assert signal_set.occupancy is None
    else:
        assert TOOL_GET_OCCUPANCY not in signal_set.missing_signals
        assert signal_set.occupancy is not None

    # Revenue: same contract as occupancy.
    if revenue_fails:
        assert TOOL_GET_REVENUE in signal_set.missing_signals
        assert signal_set.revenue is None
    else:
        assert TOOL_GET_REVENUE not in signal_set.missing_signals
        assert signal_set.revenue is not None

    # When neither required tool fails, no signal is missing and both are present.
    if not occupancy_fails and not revenue_fails:
        assert signal_set.missing_signals == ()
        assert signal_set.occupancy is not None
        assert signal_set.revenue is not None

    # The derived partial-data flag: downstream sets based_on_partial_data when a
    # REQUIRED signal (occupancy/revenue) is present in missing_signals (Req 2.8).
    based_on_partial_data = (
        TOOL_GET_OCCUPANCY in signal_set.missing_signals
        or TOOL_GET_REVENUE in signal_set.missing_signals
    )
    # It must be True EXACTLY when a required signal was made to fail.
    assert based_on_partial_data == (occupancy_fails or revenue_fails)
