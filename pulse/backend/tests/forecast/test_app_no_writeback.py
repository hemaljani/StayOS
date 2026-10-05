"""Property test for the Forecasting Agent's advisory read + advise-only posture.

Exercises the thin orchestrator ``app.run_forecast_session`` end to end with
its collaborators injected (the Gateway ``call_tool`` seam and the alert
reconciler), so no Strands/MCP client, Bedrock, or DynamoDB is touched. The
property under test is the advisory posture of the whole run (Req 5.1, 2.5):
across arbitrary canned read-only signals, the orchestrator only ever reaches
the platform through the three READ-ONLY tools and the single ``reconcile``
write seam, and every record it hands ``reconcile`` is a ``FORECAST_OVERSELL``
item. It never invokes the Action Executor and never calls a write tool.

The reconciler is faked as a recording callable matching ``reconcile_alerts``'
keyword signature. Because it is the only write path exposed to the
orchestrator, recording what it receives (and asserting each item's ``type``)
structurally proves the run's only write is the advisory ``pulse-alerts`` write
- there is no executor seam to invoke.

Validates: Requirements 5.1, 2.5.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

import app
from forecast.alerts import FORECAST_ALERT_TYPE, ReconcileResult, UpsertResult
from forecast.signals import (
    TOOL_GET_OCCUPANCY,
    TOOL_GET_REVENUE,
    TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
)

from .conftest import RecordingToolCaller

# The orchestration is heavier than a pure-engine property (it sequences read ->
# forecast -> author -> reconcile), and the actionable path exercises the
# Remediation Plan Author's deterministic template fallback, whose per-example
# cost varies. max_examples is lowered to 50 and the per-example deadline is
# disabled so that variable-but-correct timing is not mistaken for a failure.
PROPERTY_SETTINGS = settings(max_examples=50, deadline=None)

# The three tools the advisory agent is permitted to reach (Req 2.5, 5.1). Any
# recorded tool name outside this set would be a write/executor tool.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        TOOL_GET_OCCUPANCY,
        TOOL_GET_REVENUE,
        TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
    }
)

# The enabled allowlist set by the conftest ``_forecast_env`` fixture. A bound
# propertyId must come from this set so the invocation gate accepts the run.
ENABLED_PROPERTY_IDS: tuple[str, ...] = (
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)


class RecordingReconciler:
    """A recording fake matching ``forecast.alerts.reconcile_alerts``' signature.

    Stands in for the only write path the orchestrator has (the advisory
    ``pulse-alerts`` write). It records every prediction handed to it so the test
    can assert each carries a ``FORECAST_OVERSELL`` item, and returns a
    well-formed :class:`ReconcileResult` so the orchestrator can build its
    summary without any DynamoDB.

    Attributes:
        predictions: The prediction mappings received across all calls, flattened
            in call order.
        call_count: How many times the reconciler was invoked.
    """

    def __init__(self) -> None:
        """Initialize with an empty record of received predictions."""
        self.predictions: list[dict[str, Any]] = []
        self.call_count: int = 0

    def __call__(
        self,
        predictions: Any,
        *,
        property_id: str,
        alerts_table_name: str,
        now: Any = None,
        realtime_publisher: Any = None,
        **_extra: Any,
    ) -> ReconcileResult:
        """Record the predictions and return a well-formed ReconcileResult."""
        self.call_count += 1
        received = list(predictions)
        self.predictions.extend(received)
        # One "created" upsert per received prediction so predictionsWritten in
        # the run summary reflects the number of advisory records written.
        upserts = [
            UpsertResult(created=True, alert_id=f"alert-{index}")
            for index, _prediction in enumerate(received)
        ]
        return ReconcileResult(upserts=upserts, resolved=[])


def _occupancy_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap per-date occupancy rows in the ``records`` envelope the reader unwraps."""
    return {"records": rows}


# ---------------------------------------------------------------------------
# Property 23: Forecasting performs no write-back and never invokes the executor
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 23: Forecasting performs no write-back and never invokes the executor
@PROPERTY_SETTINGS
@given(
    property_id=st.sampled_from(ENABLED_PROPERTY_IDS),
    # Per-date occupancy rows that may or may not imply an oversell: arriving +
    # occupied vs sellable capacity spans well under and well over capacity so
    # both the no-alert and the actionable-prediction paths are exercised.
    occupancy_rows=st.lists(
        st.fixed_dictionaries(
            {
                "date": st.dates().map(lambda value: value.isoformat()),
                "occupiedRooms": st.integers(min_value=0, max_value=200),
                "arrivingRooms": st.integers(min_value=0, max_value=200),
                "sellableCapacity": st.integers(min_value=1, max_value=150),
            }
        ),
        min_size=0,
        max_size=8,
    ),
    # A canned sister-availability result for the two-phase overflow read.
    sister_rooms=st.integers(min_value=0, max_value=100),
)
def test_property_23_no_writeback_never_invokes_executor(
    property_id: str,
    occupancy_rows: list[dict[str, Any]],
    sister_rooms: int,
) -> None:
    """Advisory posture: reads are read-only and the only write is via reconcile.

    For any bound (enabled) property and any canned read-only signals, asserts:
    - EVERY tool name recorded by the Gateway seam is one of the three READ-ONLY
      tools - never a write/executor tool (Req 2.5, 5.1);
    - the injected ``reconcile`` fake is the ONLY write path used, and every item
      handed to it is a ``FORECAST_OVERSELL`` item (there is no executor seam to
      invoke, so recording reconcile's inputs structurally proves the only write
      is the advisory pulse-alerts write, Req 5.1);
    - the returned summary is well-formed and no exception escapes.
    """
    # Canned read-only results for all three tools. The reader scopes each row to
    # the bound property, so echo the bound propertyId onto every record.
    scoped_rows = [
        {**row, "propertyId": property_id} for row in occupancy_rows
    ]
    call_tool = RecordingToolCaller(
        {
            TOOL_GET_OCCUPANCY: _occupancy_records(scoped_rows),
            TOOL_GET_REVENUE: {"records": []},
            TOOL_GET_SISTER_PROPERTY_AVAILABILITY: {
                "records": [
                    {
                        "propertyId": "ALOHA-OTHER-001",
                        "availableRooms": sister_rooms,
                    }
                ]
            },
        }
    )
    reconcile = RecordingReconciler()

    summary = app.run_forecast_session(
        {"propertyId": property_id},
        call_tool=call_tool,
        reconcile=reconcile,
    )

    # 1. Every tool the run reached is a read-only tool - no write/executor tool.
    for tool_name in call_tool.tool_names():
        assert tool_name in READ_ONLY_TOOL_NAMES, (
            f"orchestrator called a non-read-only tool: {tool_name!r}"
        )

    # 2. The injected reconcile fake is the only write path, and every item it
    #    was handed is a FORECAST_OVERSELL advisory item (Req 5.1). There is no
    #    executor seam on the orchestrator to invoke.
    for prediction in reconcile.predictions:
        item = prediction["item"]
        assert item["type"] == FORECAST_ALERT_TYPE

    # 3. The run summary is well-formed and no exception escaped. A gated run
    #    should never be rejected (the property is enabled) or fail.
    assert summary["propertyId"] == property_id
    assert "rejected" not in summary
    assert "failed" not in summary
    assert "predictionsWritten" in summary
    assert "resolved" in summary
    assert "noAlert" in summary
    # predictionsWritten reflects exactly the advisory items handed to reconcile.
    assert summary["predictionsWritten"] == len(reconcile.predictions)
