"""Property test for the Signal Reader's read-only tool posture (Req 2.5).

Covers the read-only guarantee of ``forecast.signals.read_signals``: across a
full two-phase read (occupancy + revenue always, sister availability only when
overflow evaluation is needed), the reader may ONLY ever invoke the three
READ-ONLY tools in ``READ_ONLY_TOOLS`` - it must never reach a write tool. This
is exercised with Hypothesis over the ``needs_overflow`` flag (the only input
that changes which tools are called), asserting the recorded call set against
the read-only allowlist. The private guard ``_call_read_only_tool`` is also
driven directly to prove it refuses a non-read-only tool name by raising
``NonReadOnlyToolError`` before ever touching the seam.

The reader is exercised through the injected ``ToolCaller`` seam (the conftest
``RecordingToolCaller``), with the timeout runner and backoff sleep stubbed so
no real threads or waits occur, keeping the property test fast and deterministic.

Validates: Requirements 2.5.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import build_forecast_horizon
from forecast.signals import (
    READ_ONLY_TOOLS,
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
    NonReadOnlyToolError,
    _call_read_only_tool,
    read_signals,
)

from .conftest import RecordingToolCaller

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The single bound run property every signal is read for (Req 6.1). Records in
# the canned tool results carry this id so nothing is dropped by the isolation
# filter - this property is about WHICH tools are called, not filtering.
RUN_PROPERTY_ID = "ALOHA-CHI-001"

# A write tool name that is deliberately NOT a member of READ_ONLY_TOOLS, used to
# drive the guard's rejection path directly.
WRITE_TOOL_NAME = "create_alert"


def _no_thread_timeout_runner(thunk, _timeout_seconds):
    """Run the thunk inline, bypassing the real thread-based timeout runner."""
    return thunk()


def _no_sleep(_seconds):
    """No-op backoff sleep so retries impose no wall-clock delay."""
    return None


def _canned_results() -> dict:
    """Build canned read-only tool results, all scoped to the bound property."""
    # Each result carries the bound propertyId so the isolation filter keeps it;
    # the read-only property does not depend on record contents.
    record = {"propertyId": RUN_PROPERTY_ID, "availableRooms": 5}
    return {
        TOOL_GET_OCCUPANCY: [record],
        TOOL_GET_REVENUE: [record],
        TOOL_GET_SISTER_PROPERTY_AVAILABILITY: [record],
    }


# ---------------------------------------------------------------------------
# Property 4: Only the read-only reuse tools are ever invoked
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 4: Only the read-only reuse tools are ever invoked
@PROPERTY_SETTINGS
@given(needs_overflow=st.booleans())
def test_property_4_only_read_only_tools_invoked(needs_overflow: bool) -> None:
    """Only read-only tools are ever called across the two-phase read (Req 2.5).

    For any ``needs_overflow`` flag, asserts:
    - every recorded tool name is a member of ``READ_ONLY_TOOLS`` (never a write
      tool);
    - the set of tools called is a subset of the three read-only tools;
    - ``get_occupancy`` and ``get_revenue`` are always called, and
      ``get_sister_property_availability`` is called if and only if overflow
      evaluation was requested.
    """
    caller = RecordingToolCaller(_canned_results())
    horizon = build_forecast_horizon(date(2025, 1, 1), 14)

    read_signals(
        RUN_PROPERTY_ID,
        horizon,
        caller,
        needs_overflow=needs_overflow,
        timeout_runner=_no_thread_timeout_runner,
        sleep=_no_sleep,
    )

    called_names = caller.tool_names()

    # Every recorded tool name is read-only - a write tool never appears (Req 2.5).
    for tool_name in called_names:
        assert tool_name in READ_ONLY_TOOLS

    # The tools called are a subset of the read-only allowlist.
    assert set(called_names).issubset(READ_ONLY_TOOLS)

    # Occupancy and revenue are always read; sister availability only on overflow.
    assert TOOL_GET_OCCUPANCY in called_names
    assert TOOL_GET_REVENUE in called_names
    assert (TOOL_GET_SISTER_PROPERTY_AVAILABILITY in called_names) is needs_overflow


# Feature: predictive-forecasting-agent, Property 4: Only the read-only reuse tools are ever invoked
def test_guard_refuses_non_read_only_tool() -> None:
    """The guard rejects a write tool before touching the seam (Req 2.5).

    Directly exercises the private ``_call_read_only_tool`` guard: invoking it
    with a tool name outside ``READ_ONLY_TOOLS`` must raise
    ``NonReadOnlyToolError`` and must NOT call the seam even once.
    """
    caller = RecordingToolCaller(_canned_results())

    with pytest.raises(NonReadOnlyToolError):
        _call_read_only_tool(
            caller,
            WRITE_TOOL_NAME,
            RUN_PROPERTY_ID,
            timeout_runner=_no_thread_timeout_runner,
            sleep=_no_sleep,
        )

    # The seam was never touched - the guard rejects before any invocation.
    assert caller.tool_names() == []
