"""Example (unit) tests for the Forecasting Agent thin entrypoint (task 10.3).

Covers the ``app.run_forecast_session`` orchestrator - the thin gate + sequence
that binds one ``propertyId`` to a run, reads signals over the injected Gateway
seam, runs the pure engine, and reconciles alerts. These are plain example tests
(not property tests) exercising the four orchestration behaviours the design's
Testing Strategy -> Example/unit tests calls out, with the Gateway mocked via the
conftest ``RecordingToolCaller`` and the alert reconciler replaced by an injected
fake so nothing touches Bedrock, a Strands/MCP client, or DynamoDB:

1. Pass-through / bound (Req 1.4): the bound ``propertyId`` from the session
   input is threaded onto every Gateway tool call and into the reconciler, and a
   no-oversell run returns a success summary with ``noAlert`` True.
2. No-alert success (Req 8.4): when no prediction meets the threshold the run is
   a healthy success (``noAlert`` True, ``predictionsWritten`` == 0), not a
   failure.
3. Rejected invocation (Req 1.4, 1.5): a missing / unknown ``propertyId`` is
   rejected BEFORE any read or write - neither the tool caller nor the reconciler
   is ever touched.
4. Error containment (Req 8.3): a collaborator that raises is caught and returned
   as a structured failure summary; the exception never propagates.

Validates: Requirements 1.4, 8.3, 8.4.
"""

from __future__ import annotations

from typing import Any, Optional

# conftest.py puts the forecast-agent service dir on sys.path so `import app`
# and `import forecast.*` resolve, and sets FORECAST_ENABLED_PROPERTY_IDS to
# include ALOHA-CHI-001.
import app  # noqa: E402
from forecast.alerts import ReconcileResult, UpsertResult  # noqa: E402
from forecast.config import load_config  # noqa: E402
from forecast.signals import (  # noqa: E402
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
)

from .conftest import RecordingToolCaller  # noqa: E402

# The bound property under test; present in the conftest enabled allowlist.
BOUND_PROPERTY_ID = "ALOHA-CHI-001"

# A deterministic ISO timestamp so the reconciler seam receives a fixed `now`.
FIXED_NOW = "2026-08-17T14:33:10+00:00"


def _fixed_now() -> str:
    """Return a fixed ISO 8601 timestamp for the reconciler seam.

    Returns:
        The constant :data:`FIXED_NOW`, so the orchestration is deterministic.
    """
    return FIXED_NOW


def _healthy_occupancy() -> dict[str, Any]:
    """Build a canned ``get_occupancy`` result with no oversell.

    Each forward date has demand (occupied + arriving) comfortably below sellable
    capacity, so the pure engine produces no oversell prediction and the run is a
    healthy no-alert outcome.

    Returns:
        A demo-shaped occupancy result enveloping per-date records.
    """
    records = [
        {
            "date": f"2026-08-{day:02d}",
            "occupiedRooms": 40,
            "arrivingRooms": 5,
            "sellableCapacity": 100,
        }
        for day in range(18, 32)
    ]
    return {"records": records}


def _healthy_revenue() -> dict[str, Any]:
    """Build a canned ``get_revenue`` result (pace not decomposed by the engine).

    Returns:
        A demo-shaped revenue result; its exact shape is not asserted on, it
        simply needs to be a well-formed canned value for the second read.
    """
    return {"records": [{"date": "2026-08-18", "adr": 210.0, "revPar": 95.0}]}


class RecordingReconciler:
    """A fake alert reconciler matching ``forecast.alerts.reconcile_alerts``.

    Records the predictions and keyword arguments it is called with so the tests
    can assert the write set (and that it was called with the bound property and
    an empty prediction list on a no-oversell run) without touching DynamoDB.

    Attributes:
        calls: Recorded ``(predictions, kwargs)`` tuples, in call order.
        result: The :class:`ReconcileResult` every call returns.
    """

    def __init__(self, result: Optional[ReconcileResult] = None) -> None:
        """Initialize with the canned result to return on each call.

        Args:
            result: The reconcile outcome to return. Defaults to an empty
                no-write result (no upserts, nothing resolved).
        """
        self.calls: list[tuple[list[Any], dict[str, Any]]] = []
        self.result = result if result is not None else ReconcileResult(
            upserts=[], resolved=[]
        )

    def __call__(
        self,
        predictions: Any,
        *,
        property_id: str,
        alerts_table_name: str,
        now: Optional[str] = None,
        realtime_publisher: Any = None,
    ) -> ReconcileResult:
        """Record the reconcile call and return the canned result.

        Signature-compatible with ``forecast.alerts.reconcile_alerts``.

        Args:
            predictions: The threshold-meeting predictions to write.
            property_id: The bound run's Property identifier.
            alerts_table_name: The ``pulse-alerts`` physical table name.
            now: The ISO 8601 timestamp for update/resolve timestamps.
            realtime_publisher: The injectable realtime publisher seam.

        Returns:
            The canned :class:`ReconcileResult`.
        """
        self.calls.append(
            (
                list(predictions),
                {
                    "property_id": property_id,
                    "alerts_table_name": alerts_table_name,
                    "now": now,
                    "realtime_publisher": realtime_publisher,
                },
            )
        )
        return self.result


def _healthy_tool_caller() -> RecordingToolCaller:
    """Build a recording Gateway caller returning the no-oversell canned reads.

    Returns:
        A :class:`RecordingToolCaller` wired for occupancy and revenue (the
        two phase-1 reads); a no-oversell run never reaches the sister read.
    """
    return RecordingToolCaller(
        {
            TOOL_GET_OCCUPANCY: _healthy_occupancy(),
            TOOL_GET_REVENUE: _healthy_revenue(),
        }
    )


def test_bound_property_threaded_through_reads_and_reconcile() -> None:
    """Pass-through / bound: the session propertyId binds every call (Req 1.4).

    A ``{"propertyId": "ALOHA-CHI-001"}`` session with no-oversell signals returns
    a success summary bound to that id with ``noAlert`` True; the reconciler was
    called once with an empty prediction list and the bound property; and every
    Gateway tool call carried that same propertyId.
    """
    config = load_config()
    caller = _healthy_tool_caller()
    reconciler = RecordingReconciler()

    summary = app.run_forecast_session(
        {"propertyId": BOUND_PROPERTY_ID},
        call_tool=caller,
        config=config,
        reconcile=reconciler,
        now_fn=_fixed_now,
    )

    # Success summary bound to the requested property, with no alert written.
    assert summary["propertyId"] == BOUND_PROPERTY_ID
    assert summary["noAlert"] is True
    assert "failed" not in summary and "rejected" not in summary

    # The reconciler was called exactly once, scoped to the bound property and
    # with an empty prediction list (no oversell met the threshold).
    assert len(reconciler.calls) == 1
    predictions, kwargs = reconciler.calls[0]
    assert predictions == []
    assert kwargs["property_id"] == BOUND_PROPERTY_ID
    assert kwargs["now"] == FIXED_NOW

    # Every recorded tool call carried the bound propertyId (data isolation).
    assert caller.calls, "expected at least the phase-1 reads"
    assert all(pid == BOUND_PROPERTY_ID for pid in caller.property_ids())


def test_no_alert_outcome_is_a_healthy_success() -> None:
    """No-alert success: no threshold-meeting prediction is healthy (Req 8.4).

    When nothing meets the write threshold the summary reports ``noAlert`` True
    and ``predictionsWritten`` == 0 - a healthy outcome, not a contained failure.
    """
    config = load_config()
    caller = _healthy_tool_caller()
    reconciler = RecordingReconciler()

    summary = app.run_forecast_session(
        {"propertyId": BOUND_PROPERTY_ID},
        call_tool=caller,
        config=config,
        reconcile=reconciler,
        now_fn=_fixed_now,
    )

    assert summary["noAlert"] is True
    assert summary["predictionsWritten"] == 0
    assert summary["resolved"] == 0
    # Not a failure and not a rejection.
    assert "failed" not in summary and "rejected" not in summary


def test_rejected_invocation_touches_no_read_or_write() -> None:
    """Rejected invocation: an unknown/missing id reads/writes nothing (Req 1.4/1.5).

    A session missing ``propertyId`` is rejected by the invocation gate BEFORE any
    read or write: the summary carries ``rejected`` True with a reason, and neither
    the Gateway tool caller nor the reconciler was ever touched.
    """
    config = load_config()
    caller = _healthy_tool_caller()
    reconciler = RecordingReconciler()

    summary = app.run_forecast_session(
        {},  # no propertyId at all -> gate rejects
        call_tool=caller,
        config=config,
        reconcile=reconciler,
        now_fn=_fixed_now,
    )

    assert summary["rejected"] is True
    assert summary["propertyId"] is None
    assert isinstance(summary["reason"], str) and summary["reason"]

    # The gate rejected before any read or write: both seams stay untouched.
    assert caller.calls == []
    assert reconciler.calls == []


def test_reconcile_failure_is_contained() -> None:
    """Error containment: a raising collaborator is caught (Req 8.3).

    When the injected reconciler raises, ``run_forecast_session`` catches it,
    returns a structured failure summary bound to the property, and does NOT
    propagate the exception.
    """
    config = load_config()
    caller = _healthy_tool_caller()

    def _raising_reconcile(
        predictions: Any,
        *,
        property_id: str,
        alerts_table_name: str,
        now: Optional[str] = None,
        realtime_publisher: Any = None,
    ) -> ReconcileResult:
        """A reconciler seam that always raises to exercise containment."""
        raise RuntimeError("reconcile blew up (test)")

    # The exception must be contained, not propagated.
    summary = app.run_forecast_session(
        {"propertyId": BOUND_PROPERTY_ID},
        call_tool=caller,
        config=config,
        reconcile=_raising_reconcile,
        now_fn=_fixed_now,
    )

    assert summary["failed"] is True
    assert summary["propertyId"] == BOUND_PROPERTY_ID
    # The reason is the caught exception's type name (see run_forecast_session).
    assert summary["reason"] == "RuntimeError"
    assert "predictionsWritten" not in summary


def test_success_summary_reports_written_and_resolved_counts() -> None:
    """A reconcile that writes surfaces its counts and clears noAlert (Req 8.4).

    Complements the no-alert case: when the reconciler reports an upsert, the
    summary reflects ``predictionsWritten`` and ``resolved`` from the result and
    ``noAlert`` is False. This pins the summary's count plumbing without needing
    the engine to produce an oversell.
    """
    config = load_config()
    caller = _healthy_tool_caller()
    reconciler = RecordingReconciler(
        ReconcileResult(
            upserts=[UpsertResult(created=True, alert_id="alert-1")],
            resolved=[{"alertId": "alert-2"}],
        )
    )

    summary = app.run_forecast_session(
        {"propertyId": BOUND_PROPERTY_ID},
        call_tool=caller,
        config=config,
        reconcile=reconciler,
        now_fn=_fixed_now,
    )

    assert summary["predictionsWritten"] == 1
    assert summary["resolved"] == 1
    assert summary["noAlert"] is False
