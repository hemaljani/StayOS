"""Property test for the Alert Writer's threshold-gated, property-scoped write.

Covers the write DECISION the orchestrator makes over the Alert Writer
(``forecast.alerts``): a predictive record is written if and only if a
prediction's confidence meets the configured threshold
(``meets_threshold(confidence, threshold)``), and every written record is scoped
to the bound run's ``propertyId`` (Req 6.2). The gate itself is the pure
``confidence >= threshold`` predicate (Req 4.1, 4.8); the write goes through the
injectable ``table_getter`` seam so a ``FakeAlertsTable`` records the put without
any real DynamoDB client.

The test models the orchestrator's control flow directly -- ``if
meets_threshold(...): assemble + upsert`` -- and asserts the observable outcome
on the fake table:
- confidence >= threshold  -> exactly one create/put, scoped to the bound
  property, carrying forecast + dedupeKey;
- confidence <  threshold  -> zero puts and zero updates (no write at all).

Because a fresh ``FakeAlertsTable`` with empty ``query_items`` is used per
example, the upsert always takes the create/put path (no existing open record).

Validates: Requirements 4.1, 4.8, 6.2.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    build_forecast_alert_item,
    derive_dedupe_key,
    meets_threshold,
    upsert_forecast_alert,
)
from forecast.narrative import author_remediation_plan

from .conftest import FakeAlertsTable

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The bound run's Property identifier every written record must be scoped to
# (Req 6.2). A single constant so the "never a different propertyId" assertion
# has one reference value.
BOUND_PROPERTY_ID: str = "ALOHA-CHI-001"

# A fixed anticipated oversold day for the assembled item / dedupeKey. Held
# constant so the property isolates the confidence-vs-threshold gate rather than
# date variation.
ANTICIPATED_DATE: str = "2025-06-15"

# The physical alerts table name (matches the conftest ALERTS_TABLE_NAME env).
ALERTS_TABLE_NAME: str = "pulse-alerts"


def _assemble_item(confidence: int, threshold: int, dedupe_key: str) -> dict[str, Any]:
    """Assemble a FORECAST_OVERSELL item scoped to the bound property.

    Builds a template RemediationPlan (``model_id=None`` -> deterministic
    template, no Bedrock call) and assembles the ``pulse-alerts`` item for the
    bound run's property, with an injected fixed ``alert_id`` / ``now`` so the
    create/put path is deterministic.

    Args:
        confidence: The prediction confidence (integer in [0, 100]).
        threshold: The configured minimum confidence; used to tier severity.
        dedupe_key: The deterministic dedupeKey to embed in the item.

    Returns:
        The assembled ``pulse-alerts`` item scoped to ``BOUND_PROPERTY_ID``.
    """
    rooms_oversold = 3
    # model_id=None + no invoker -> deterministic TEMPLATE plan (no Bedrock).
    plan = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=ANTICIPATED_DATE,
        confidence=confidence,
        model_id=None,
    )
    return build_forecast_alert_item(
        property_id=BOUND_PROPERTY_ID,
        plan=plan,
        rooms_oversold=rooms_oversold,
        anticipated_date=ANTICIPATED_DATE,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=False,
        threshold=threshold,
        alert_id="fixed-alert-id",
        now="2025-06-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Property 14: Alert is written iff confidence meets the threshold, scoped to
# the run property.
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 14: Alert is written if and only if confidence meets the threshold, scoped to the run property
@PROPERTY_SETTINGS
@given(
    confidence=st.integers(min_value=0, max_value=100),
    threshold=st.integers(min_value=0, max_value=100),
)
def test_property_14_alert_written_iff_confidence_meets_threshold(
    confidence: int, threshold: int
) -> None:
    """A write happens iff confidence >= threshold, always scoped to the property.

    Models the orchestrator's gate directly: only ``assemble + upsert`` when
    ``meets_threshold(confidence, threshold)``; otherwise do nothing. Asserts on
    a fresh fake table (empty query -> create/put path):
    - confidence >= threshold: exactly one put, zero updates, and the written
      item's ``propertyId`` equals the bound run property (Req 6.2), with the
      forecast + dedupeKey present;
    - confidence < threshold: zero puts and zero updates (no write at all);
    - every written record is scoped to the single bound propertyId.
    """
    # Fresh, empty fake table per example so the upsert takes the create/put
    # path (no existing open record to update in place).
    fake_table = FakeAlertsTable(query_items=[])

    dedupe_key = derive_dedupe_key(BOUND_PROPERTY_ID, "OVERSELL", ANTICIPATED_DATE)
    should_write = meets_threshold(confidence, threshold)

    # Encode the orchestrator's write gate: assemble + upsert ONLY when the
    # confidence meets the threshold; otherwise no write occurs.
    if meets_threshold(confidence, threshold):
        item = _assemble_item(confidence, threshold, dedupe_key)
        upsert_forecast_alert(
            item,
            dedupe_key=dedupe_key,
            property_id=BOUND_PROPERTY_ID,
            alerts_table_name=ALERTS_TABLE_NAME,
            now="2025-06-01T00:00:00+00:00",
            table_getter=lambda _name: fake_table,
        )

    total_writes = len(fake_table.puts) + len(fake_table.updates)

    if should_write:
        # Confidence meets the threshold -> exactly one create (put), no update.
        assert should_write is True
        assert confidence >= threshold
        assert len(fake_table.puts) == 1
        assert len(fake_table.updates) == 0
        assert total_writes == 1

        # The single written record is scoped to the bound run property (Req 6.2)
        # and carries the forecast + dedupeKey.
        written_item = fake_table.puts[0]["Item"]
        assert written_item["propertyId"] == BOUND_PROPERTY_ID
        assert written_item["dedupeKey"] == dedupe_key
        assert "forecast" in written_item
    else:
        # Confidence below the threshold -> no write at all (Req 4.1, 4.8).
        assert should_write is False
        assert confidence < threshold
        assert len(fake_table.puts) == 0
        assert len(fake_table.updates) == 0
        assert total_writes == 0

    # Whatever was written (0 or 1 record) is ALWAYS scoped to the single bound
    # propertyId, never a different one (Req 6.2).
    for put_call in fake_table.puts:
        assert put_call["Item"]["propertyId"] == BOUND_PROPERTY_ID
