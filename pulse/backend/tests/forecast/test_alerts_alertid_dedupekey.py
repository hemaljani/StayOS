"""Property test for the Alert Writer's alertId / dedupeKey on the written record.

Covers the create path of the Forecasting Agent Alert Writer
(``forecast.alerts.upsert_forecast_alert`` over an item assembled by
``forecast.alerts.build_forecast_alert_item``): when a threshold-meeting oversell
prediction has no existing open record, exactly one ``put_item`` is written, and
the persisted record carries a valid ``alertId`` (a parseable uuid4 when
generated, or the explicitly provided id) plus the deterministic ``dedupeKey``
derived by :func:`forecast.alerts.derive_dedupe_key` in the literal
``forecast#{propertyId}#OVERSELL#{YYYY-MM-DD}`` form.

The DynamoDB boundary is faked with the shared ``FakeAlertsTable`` (records
``.puts``), injected via ``table_getter`` so no real boto3 client or network
call happens. The RemediationPlan is built through the model-free TEMPLATE path
(``author_remediation_plan(model_id=None)``).

Validates: Requirements 4.3.
"""

from __future__ import annotations

import uuid
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    DEFAULT_CONDITION_TYPE,
    build_forecast_alert_item,
    derive_dedupe_key,
    upsert_forecast_alert,
)
from forecast.narrative import author_remediation_plan

from .conftest import FakeAlertsTable

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The configured write threshold (mirrors the conftest env default). Generated
# confidences are >= this so the assembled prediction always warrants a write.
THRESHOLD = 50


def _is_uuid(value: str) -> bool:
    """Return whether ``value`` parses as a canonical UUID string."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Property 16: Written record carries a valid alertId and the derived dedupeKey
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 16: Written record carries a valid alertId and the derived dedupeKey
@PROPERTY_SETTINGS
@given(
    # A non-empty, whitespace-free property id (the data-isolation boundary).
    property_id=st.text(
        alphabet=st.characters(
            min_codepoint=48, max_codepoint=90, whitelist_categories=("Lu", "Nd")
        ),
        min_size=1,
        max_size=24,
    ).filter(lambda value: value.strip() == value and value != ""),
    # An actionable shortfall of at least one room.
    rooms_oversold=st.integers(min_value=1, max_value=50),
    # Confidence at or above the threshold so a write happens; capped at 100.
    confidence=st.integers(min_value=THRESHOLD, max_value=100),
    # The anticipated oversold date, emitted as an ISO YYYY-MM-DD string.
    anticipated_date=st.dates(
        min_value=date(2024, 1, 1), max_value=date(2100, 12, 31)
    ),
    # Either let the writer generate a uuid4 alertId, or provide an explicit one.
    provided_alert_id=st.one_of(st.none(), st.uuids().map(str)),
    based_on_partial_data=st.booleans(),
)
def test_property_16_written_record_alertid_and_dedupekey(
    property_id: str,
    rooms_oversold: int,
    confidence: int,
    anticipated_date: date,
    provided_alert_id: str | None,
    based_on_partial_data: bool,
) -> None:
    """A create writes exactly one record with a valid alertId + derived dedupeKey.

    For any generated actionable prediction (rooms_oversold >= 1, confidence >=
    threshold) upserted into a fresh empty table:
    - exactly one ``put_item`` is recorded;
    - the written ``alertId`` is a non-empty string that is either a parseable
      UUID (when generated) or equals the explicitly provided id;
    - the written ``dedupeKey`` equals ``derive_dedupe_key(...)`` and matches the
      literal ``forecast#...#OVERSELL#...`` format;
    - the assembled item and the persisted put agree on both fields.
    """
    anticipated_iso = anticipated_date.isoformat()

    # Deterministic dedupeKey for this (property, OVERSELL, day).
    dedupe_key = derive_dedupe_key(
        property_id, DEFAULT_CONDITION_TYPE, anticipated_iso
    )

    # Model-free TEMPLATE remediation plan (no Bedrock dependency); its numbers
    # equal the inputs and are never recomputed.
    plan = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_iso,
        confidence=confidence,
        model_id=None,
    )

    # Assemble the FORECAST_OVERSELL item, carrying the candidate alertId.
    item = build_forecast_alert_item(
        property_id=property_id,
        plan=plan,
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_iso,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=based_on_partial_data,
        threshold=THRESHOLD,
        alert_id=provided_alert_id,
    )

    # Fresh empty table => no open record => the writer takes the CREATE path.
    fake = FakeAlertsTable()
    result = upsert_forecast_alert(
        item,
        dedupe_key=dedupe_key,
        property_id=property_id,
        alerts_table_name="pulse-alerts",
        table_getter=lambda _name: fake,
    )

    # Exactly one put was recorded (a single new record was written).
    assert result.created is True
    assert len(fake.puts) == 1
    written_item = fake.puts[0]["Item"]

    # alertId on the written record is a non-empty string and is valid: a
    # parseable UUID when generated, or exactly the provided id.
    written_alert_id = written_item["alertId"]
    assert isinstance(written_alert_id, str)
    assert written_alert_id != ""
    if provided_alert_id is None:
        assert _is_uuid(written_alert_id)
    else:
        assert written_alert_id == provided_alert_id

    # dedupeKey on the written record equals the derived key and matches the
    # literal forecast#...#OVERSELL#... format.
    written_dedupe_key = written_item["dedupeKey"]
    assert written_dedupe_key == dedupe_key
    assert written_dedupe_key == (
        f"forecast#{property_id}#OVERSELL#{anticipated_iso}"
    )

    # The assembled item and the persisted put agree on both identity fields,
    # and the upsert result's alertId matches what was written.
    assert written_alert_id == item["alertId"]
    assert written_dedupe_key == item["dedupeKey"]
    assert result.alert_id == written_alert_id
