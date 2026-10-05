"""Property test for the Alert Writer update-in-place content overwrite.

Covers the update-in-place branch of the Alert Writer
(``forecast.alerts.upsert_forecast_alert``): when an existing OPEN predictive
record with the same ``(dedupeKey, propertyId)`` is found via the
``propertyId-status-index`` GSI query, the upsert issues a single
``update_item`` that OVERWRITES the mutable content -- the whole ``forecast``
sub-object (confidence, condition fields, recommendation), ``severity``/``tier``,
and ``lastStatusChangeAt`` -- while RETAINING the record's identity: ``dedupeKey``,
``alertId``, and ``createdAt`` are never written in the update (Req 7.3, 7.4).

The write path is exercised through the ``FakeAlertsTable`` seam (from the
shared conftest), which returns its ``query_items`` from ``query()`` and records
every ``update_item`` call under ``.updates``, so the test never opens a network
connection or touches DynamoDB. The candidate item and its RemediationPlan are
built with the real ``build_forecast_alert_item`` / ``derive_dedupe_key`` /
``author_remediation_plan(model_id=None)`` helpers (the template fallback path,
so no model is invoked). The OLD open record carries a different confidence and
recommendation than the NEW candidate run, so the overwrite is observable.

Validates: Requirements 7.3, 7.4.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

# The identity/content attribute keys touched (or intentionally not touched) by
# the update path. Imported so the assertions reference the same camelCase
# attrs the module writes (NAMING-05).
from forecast.alerts import (
    ATTR_ALERT_ID,
    ATTR_FORECAST,
    ATTR_LAST_STATUS_CHANGE_AT,
    ATTR_SEVERITY,
    ATTR_TIER,
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

# The confidence threshold set by the conftest env; generated confidences stay
# at or above it so every prediction clears the write gate.
THRESHOLD = 50

# The physical alerts table name set by the conftest env (never hardcoded in the
# module under test; passed explicitly here for the fake seam).
ALERTS_TABLE_NAME = "pulse-alerts"

# A fixed number of oversold rooms for the assembled item; the exact count is
# irrelevant to the content-overwrite decision.
ROOMS_OVERSOLD = 3

# Identity attributes that MUST be retained (never written) by an update.
ATTR_DEDUPE_KEY = "dedupeKey"
ATTR_CREATED_AT = "createdAt"

# The old record's frozen createdAt / lastStatusChangeAt so the retention of
# createdAt and the refresh of lastStatusChangeAt are both observable.
OLD_CREATED_AT = "2024-12-01T00:00:00+00:00"
OLD_LAST_STATUS_CHANGE_AT = "2024-12-01T00:00:00+00:00"

# The NEW run's update timestamp, distinct from the old record's timestamps so
# the lastStatusChangeAt refresh is observable.
NEW_NOW = "2025-06-15T12:00:00+00:00"


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
        now=NEW_NOW,
    )


# ---------------------------------------------------------------------------
# Property 19: Update overwrites content while preserving identity
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 19: Update overwrites content while preserving identity
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
    # Two writable confidences that MUST differ so OLD vs NEW content is
    # observably distinct. Both stay at or above the write threshold.
    confidences=st.lists(
        st.integers(min_value=THRESHOLD, max_value=100),
        min_size=2,
        max_size=2,
        unique=True,
    ),
)
def test_property_19_update_overwrites_content_preserving_identity(
    property_id: str, anticipated_date: date, confidences: list[int]
) -> None:
    """Update overwrites forecast/severity/tier/timestamp; retains identity.

    Asserts, for any property, anticipated day, and two distinct writable
    confidences (an OLD open record's confidence and a NEW run's confidence):
    - the recorded ``update_item`` targets the EXISTING record's ``alertId`` Key
      (identity preserved), not the fresh candidate id;
    - the update SETs the NEW ``forecast`` sub-object (new confidence and new
      recommendation), the NEW ``severity``/``tier``, and the NEW
      ``lastStatusChangeAt``;
    - the update does NOT write ``dedupeKey``, ``alertId``, or ``createdAt`` --
      neither in the SET ``UpdateExpression`` nor via the expression
      attribute names/values -- so those identity fields are retained (Req 7.3,
      7.4);
    - the result reports ``created is False`` and the retained existing id.
    """
    old_confidence, new_confidence = confidences[0], confidences[1]
    dedupe_key = derive_dedupe_key(property_id, "OVERSELL", anticipated_date)

    # A DIFFERENT candidate alertId than the retained existing id, so the
    # retained-vs-candidate distinction is observable in the update Key.
    candidate_alert_id = "candidate-alert-id"
    existing_alert_id = "existing-open-alert-id"

    # The OLD open record, carrying its OWN (different) confidence-derived
    # forecast/recommendation, its own createdAt, and an old lastStatusChangeAt.
    old_item = _build_candidate_item(
        property_id=property_id,
        anticipated_date=anticipated_date,
        confidence=old_confidence,
        dedupe_key=dedupe_key,
        alert_id=existing_alert_id,
    )
    existing_open_item = {
        ATTR_ALERT_ID: existing_alert_id,
        "propertyId": property_id,
        "type": FORECAST_ALERT_TYPE,
        "status": OPEN_ALERT_STATUS,
        ATTR_DEDUPE_KEY: dedupe_key,
        ATTR_CREATED_AT: OLD_CREATED_AT,
        ATTR_LAST_STATUS_CHANGE_AT: OLD_LAST_STATUS_CHANGE_AT,
        ATTR_FORECAST: old_item[ATTR_FORECAST],
        ATTR_SEVERITY: old_item[ATTR_SEVERITY],
        ATTR_TIER: old_item[ATTR_TIER],
    }

    # The NEW run's candidate item with a DIFFERENT confidence -> different
    # forecast sub-object (confidence + recommendation narrative differ).
    new_item = _build_candidate_item(
        property_id=property_id,
        anticipated_date=anticipated_date,
        confidence=new_confidence,
        dedupe_key=dedupe_key,
        alert_id=candidate_alert_id,
    )

    fake = FakeAlertsTable(query_items=[existing_open_item])

    result = upsert_forecast_alert(
        new_item,
        dedupe_key=dedupe_key,
        property_id=property_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        now=NEW_NOW,
        table_getter=lambda _name: fake,
    )

    # The update path ran (existing open record present) -> exactly one update.
    assert result.created is False
    assert result.alert_id == existing_alert_id
    assert len(fake.updates) == 1

    update = fake.updates[0]

    # Identity preserved: the update Key targets the EXISTING record's alertId,
    # not the fresh candidate id.
    assert update["Key"] == {ATTR_ALERT_ID: existing_alert_id}

    # The update SETs the NEW content: the whole forecast sub-object (new
    # confidence + new recommendation), the new severity/tier, and the new
    # lastStatusChangeAt.
    values = update["ExpressionAttributeValues"]
    new_forecast = new_item[ATTR_FORECAST]
    assert values[":forecast"] == new_forecast
    # The overwrite carries the NEW confidence and NEW recommendation, distinct
    # from the OLD open record's content.
    assert new_forecast["confidence"] == new_confidence
    assert new_forecast != existing_open_item[ATTR_FORECAST]
    assert values[":severity"] == new_item[ATTR_SEVERITY]
    assert values[":tier"] == new_item[ATTR_TIER]
    assert values[":ts"] == NEW_NOW
    assert NEW_NOW != OLD_LAST_STATUS_CHANGE_AT

    # Identity retained: dedupeKey / alertId / createdAt are NOT written by the
    # update -- not in the SET UpdateExpression, not in the expression attribute
    # names, and not in the expression attribute values (Req 7.3, 7.4).
    update_expression = update["UpdateExpression"]
    names = update.get("ExpressionAttributeNames", {})
    for retained_attr in (ATTR_DEDUPE_KEY, ATTR_ALERT_ID, ATTR_CREATED_AT):
        # Not referenced as a mapped attribute name in the expression.
        assert retained_attr not in names.values()
        # Not present verbatim in the SET clause text.
        assert retained_attr not in update_expression
        # Not present as any written value.
        assert retained_attr not in values
