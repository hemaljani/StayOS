"""Property test: delivery failure preserves the persisted alert (Req 4.6).

Covers the best-effort / never-raise delivery guarantee of the Alert Writer's
post-commit publish seam (``forecast.alerts.publish_alert_event``) and the
end-to-end invariant that a publish failure never rolls back or fails the
already-persisted predictive alert.

``publish_alert_event`` calls the SHARED ``pulse.delivery.realtime_publish``
helper (imported here as ``rt`` for the event-type constants) directly, so the
delivery OUTCOME is observable via the return value: ``rt.publish`` catches the
publisher's per-batch exception internally and returns ``False`` (it never
propagates), and ``publish_alert_event`` returns that bool. The observable
contract exercised here is therefore: a FAILING publisher -> returns ``False``
and never raises; an OK publisher -> returns ``True`` and never raises. On a
failure it logs a delivery-failure indication.

The realtime publisher is exercised through the ``RecordingPublisher`` seam from
``conftest`` (``fail=True`` raises when CALLED - which ``rt.publish`` catches;
``fail=False`` records the publish), and persistence through the in-memory
``FakeAlertsTable`` fake, so no network connection is opened and no real boto3
client is constructed. The engine under test is not pure here, so this mirrors
the ``test_engine_horizon.py`` style of a single Hypothesis property with
``@settings(max_examples=100)``.

Validates: Requirements 4.6.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    build_forecast_alert_item,
    derive_dedupe_key,
    publish_alert_event,
    upsert_forecast_alert,
)
from forecast.narrative import author_remediation_plan
from pulse.delivery import realtime_publish as rt

from .conftest import FakeAlertsTable, RecordingPublisher

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# The physical alerts table name the fake stands in for (matches conftest env).
ALERTS_TABLE_NAME = "pulse-alerts"

# The two event types the writer publishes post-commit (Req 4.5). Drawn from via
# Hypothesis so the never-raise guarantee holds for both.
EVENT_TYPES = [rt.EVENT_ALERT_UPDATED, rt.EVENT_ALERT_RESOLVED]

# The severity/tier tokens a forecast alert carries (Req 4.2). Generated so the
# property does not assume one fixed tier on the item under delivery.
TIER_TOKENS = ["INFO", "WARNING"]

# The predictive alert type token (Req 4.7). Held as the sole generated value so
# the property item mirrors a real FORECAST_OVERSELL item shape.
ALERT_TYPES = ["FORECAST_OVERSELL"]


# ---------------------------------------------------------------------------
# Property 28: Delivery failure preserves the persisted alert and records a
# delivery-failure indication
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 28: Delivery failure preserves the persisted alert and records a delivery-failure indication
@PROPERTY_SETTINGS
@given(
    item=st.fixed_dictionaries(
        {
            "alertId": st.text(min_size=1, max_size=40),
            "propertyId": st.text(min_size=1, max_size=40),
            "tier": st.sampled_from(TIER_TOKENS),
            "type": st.sampled_from(ALERT_TYPES),
        }
    ),
    event_type=st.sampled_from(EVENT_TYPES),
)
def test_property_28_delivery_failure_preserves_persisted_alert(
    item: dict[str, Any], event_type: str
) -> None:
    """A failing publisher returns False and never raises; a good one returns True.

    Direct-seam property (the core of Req 4.6): for any persisted-alert-like item
    (carrying ``alertId`` / ``propertyId`` / ``tier`` / ``type``) and event type:

    - ``publish_alert_event`` with a FAILING publisher returns ``False`` and does
      NOT raise. The failure surfaces through ``rt.publish`` catching the
      publisher's exception and returning ``False``, which the wrapper returns
      after logging a delivery-failure indication - the persisted alert is never
      touched by the delivery attempt.
    - ``publish_alert_event`` with a non-failing publisher returns ``True`` and
      does NOT raise, and the ``RecordingPublisher`` recorded the publish.
    """
    # Failing publisher: raises when called; rt.publish catches it and returns
    # False, so publish_alert_event returns False without ever raising.
    failing_result = publish_alert_event(
        item, event_type, realtime_publisher=RecordingPublisher(fail=True)
    )
    assert failing_result is False

    # Non-failing publisher: the publish goes through and is reported as success,
    # and the seam recorded that it was actually invoked.
    ok_publisher = RecordingPublisher(fail=False)
    ok_result = publish_alert_event(
        item, event_type, realtime_publisher=ok_publisher
    )
    assert ok_result is True
    assert len(ok_publisher.published) >= 1


# ---------------------------------------------------------------------------
# End-to-end: the persisted alert survives a failing publisher (Req 4.6)
# ---------------------------------------------------------------------------


def test_persisted_alert_survives_failing_publisher_end_to_end() -> None:
    """Persist first, then publish-fail: the write stands and nothing propagates.

    Exercises the persist-first-then-publish-best-effort ordering with a REAL
    assembled item: build a template RemediationPlan (``model_id=None`` -> no
    Bedrock call), derive the dedupeKey, and assemble the ``pulse-alerts`` item.
    Upsert it into a fresh ``FakeAlertsTable`` (empty query -> CREATE path) and
    assert exactly one put was recorded and ``result.created`` is ``True``. Then
    attempt delivery with a FAILING publisher: it must return ``False`` without
    raising, and the recorded put must be STILL present and unchanged (the
    persisted alert is preserved, not rolled back) with no extra writes.
    """
    property_id = "ALOHA-CHI-001"
    anticipated_date = "2025-01-15"
    rooms_oversold = 3
    confidence = 90
    threshold = 50

    # Deterministic template plan (model_id=None + no invoker -> no Bedrock).
    plan = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        model_id=None,
    )
    dedupe_key = derive_dedupe_key(property_id, "OVERSELL", anticipated_date)
    persisted_item = build_forecast_alert_item(
        property_id=property_id,
        plan=plan,
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=False,
        threshold=threshold,
        alert_id="alert-e2e-1",
        now="2025-01-01T00:00:00+00:00",
    )

    # No open record -> CREATE path. The fake records the single put.
    fake = FakeAlertsTable(query_items=[])

    result = upsert_forecast_alert(
        persisted_item,
        dedupe_key=dedupe_key,
        property_id=property_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        table_getter=lambda _name: fake,
    )

    # Persistence happened exactly once, via the create path, and was not rolled
    # back: exactly one put, zero updates.
    assert result.created is True
    assert result.alert_id == "alert-e2e-1"
    assert len(fake.puts) == 1
    assert len(fake.updates) == 0
    assert fake.puts[0]["Item"]["alertId"] == "alert-e2e-1"

    # Now attempt best-effort delivery with a FAILING publisher. rt.publish
    # catches the raised exception and returns False; publish_alert_event returns
    # that without raising.
    published = publish_alert_event(
        persisted_item,
        rt.EVENT_ALERT_UPDATED,
        realtime_publisher=RecordingPublisher(fail=True),
    )
    assert published is False

    # The persisted record is STILL present and unchanged after the failed
    # delivery: no rollback, no extra write.
    assert len(fake.puts) == 1
    assert len(fake.updates) == 0
    assert fake.puts[0]["Item"]["alertId"] == "alert-e2e-1"
