"""Property test for the Alert Writer upsert create-vs-update-in-place path.

Covers the only DynamoDB-touching step of the Alert Writer
(``forecast.alerts.upsert_forecast_alert``): when an existing OPEN predictive
record with the same ``(dedupeKey, propertyId)`` is found via the
``propertyId-status-index`` GSI query, the upsert UPDATES that record in place
(a single ``update_item``) and does NOT put a new record, so no duplicate is
created (Req 4.4, 7.2). When no open record exists, the upsert PUTs exactly one
new record.

The write path is exercised through the ``FakeAlertsTable`` seam (from the
shared conftest), which returns its ``query_items`` from ``query()`` and records
every ``put_item`` / ``update_item`` call, so the test never opens a network
connection or touches DynamoDB. The assembled item and RemediationPlan are built
with the real ``build_forecast_alert_item`` / ``derive_dedupe_key`` /
``author_remediation_plan(model_id=None)`` helpers (the template fallback path,
so no model is invoked).

Validates: Requirements 4.4, 7.2.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    ATTR_ALERT_ID,
    FORECAST_ALERT_TYPE,
    OPEN_ALERT_STATUS,
    build_forecast_alert_item,
    derive_dedupe_key,
    upsert_forecast_alert,
)
from forecast.narrative import author_remediation_plan

from .conftest import FakeAlertsTable

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The confidence threshold set by the conftest env; generated confidence stays
# at or above it so every prediction clears the write gate.
THRESHOLD = 50

# The physical alerts table name set by the conftest env (never hardcoded in the
# module under test; passed explicitly here for the fake seam).
ALERTS_TABLE_NAME = "pulse-alerts"

# A fixed number of oversold rooms for the assembled item; the exact count is
# irrelevant to the create-vs-update decision.
ROOMS_OVERSOLD = 3


def _build_candidate_item(
    *,
    property_id: str,
    anticipated_date: date,
    confidence: int,
    dedupe_key: str,
    alert_id: str,
) -> dict:
    """Assemble a fresh FORECAST_OVERSELL item for the upsert under test.

    Uses the real template-fallback RemediationPlan (``model_id=None``) and the
    real record assembler so the item mirrors production shape.

    Args:
        property_id: The bound run's Property identifier.
        anticipated_date: The anticipated oversold calendar day.
        confidence: The deterministic prediction confidence (>= threshold).
        dedupe_key: The deterministic dedupeKey for this prediction.
        alert_id: The candidate alert id for a NEW record.

    Returns:
        The assembled ``pulse-alerts`` item dict.
    """
    plan = author_remediation_plan(
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=anticipated_date.isoformat(),
        confidence=confidence,
        model_id=None,
    )
    return build_forecast_alert_item(
        property_id=property_id,
        plan=plan,
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=anticipated_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=False,
        threshold=THRESHOLD,
        alert_id=alert_id,
        now="2025-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Property 18: Matching open dedupeKey updates in place with no duplicate
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 18: Matching open dedupeKey updates in place with no duplicate
@PROPERTY_SETTINGS
@given(
    property_id=st.text(
        alphabet=st.characters(min_codepoint=65, max_codepoint=90),
        min_size=3,
        max_size=12,
    ),
    anticipated_date=st.dates(
        min_value=date(2025, 1, 1), max_value=date(2030, 12, 31)
    ),
    # Confidence stays at or above the write threshold so the prediction is
    # always writable; the create-vs-update decision is what is under test.
    confidence=st.integers(min_value=THRESHOLD, max_value=100),
)
def test_property_18_matching_open_dedupe_key_updates_in_place(
    property_id: str, anticipated_date: date, confidence: int
) -> None:
    """A matching open record is updated in place; a miss creates exactly one.

    Asserts, for any property, anticipated day, and writable confidence:
    - CASE existing open record present: the upsert records exactly one
      ``update_item`` and zero ``put_item`` (no duplicate), reports
      ``created is False``, and retains the existing record's ``alertId`` rather
      than the fresh candidate id (Req 4.4, 7.2);
    - CASE no existing open record: the upsert records exactly one ``put_item``
      and zero ``update_item`` and reports ``created is True``.
    """
    dedupe_key = derive_dedupe_key(property_id, "OVERSELL", anticipated_date)

    # A DIFFERENT candidate alertId than the retained existing id, so the
    # retained-vs-candidate distinction is observable.
    candidate_alert_id = "candidate-alert-id"
    existing_alert_id = "existing-open-alert-id"

    # --- CASE 1: an existing OPEN record for the same (dedupeKey, propertyId) ---
    existing_open_item = {
        ATTR_ALERT_ID: existing_alert_id,
        "propertyId": property_id,
        "type": FORECAST_ALERT_TYPE,
        "status": OPEN_ALERT_STATUS,
        "dedupeKey": dedupe_key,
        "createdAt": "2024-12-01T00:00:00+00:00",
    }
    item = _build_candidate_item(
        property_id=property_id,
        anticipated_date=anticipated_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        alert_id=candidate_alert_id,
    )
    fake_existing = FakeAlertsTable(query_items=[existing_open_item])

    result_update = upsert_forecast_alert(
        item,
        dedupe_key=dedupe_key,
        property_id=property_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        now="2025-01-01T00:00:00+00:00",
        table_getter=lambda _name: fake_existing,
    )

    # Update in place: no new record, and the existing id is retained.
    assert result_update.created is False
    assert result_update.alert_id == existing_alert_id
    # Exactly one update, zero puts -> no duplicate record created (Req 4.4, 7.2).
    assert len(fake_existing.updates) == 1
    assert fake_existing.puts == []

    # --- CASE 2: no existing open record -> a single create ---
    item_create = _build_candidate_item(
        property_id=property_id,
        anticipated_date=anticipated_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        alert_id=candidate_alert_id,
    )
    fake_empty = FakeAlertsTable(query_items=[])

    result_create = upsert_forecast_alert(
        item_create,
        dedupe_key=dedupe_key,
        property_id=property_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        now="2025-01-01T00:00:00+00:00",
        table_getter=lambda _name: fake_empty,
    )

    # Create path: exactly one put, zero updates, new-record outcome.
    assert result_create.created is True
    assert result_create.alert_id == candidate_alert_id
    assert len(fake_empty.puts) == 1
    assert fake_empty.updates == []
