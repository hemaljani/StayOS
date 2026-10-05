"""Property test for resolving open predictive alerts no longer forecast.

Covers the resolve/reconcile step of the Alert Writer
(``forecast.alerts.reconcile_resolutions``): given the set of ``dedupeKey``
values predicted THIS run, every currently-open FORECAST_OVERSELL alert whose
``dedupeKey`` is NOT in that set is transitioned to RESOLVED exactly once via a
conditional ``UpdateItem`` guarded on the record still being open, while open
alerts whose key IS still predicted are left untouched. A conditional-check
failure (already terminal / race) is treated as "already resolved, skip" so the
resolve happens exactly once and never raises.

These properties are exercised over the pure diff/resolve logic with an
in-memory ``FakeAlertsTable`` (its ``query`` returns the open forecast items and
its ``updates`` record the conditional writes) and a ``RecordingPublisher`` so
publish never errors. The fake ``query`` returns exactly what it is given, which
reflects the server-side FORECAST_OVERSELL type filter in
``_find_open_forecast_alerts`` (only forecast items are supplied here).

Validates: Requirements 7.5.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    FORECAST_ALERT_TYPE,
    OPEN_ALERT_STATUS,
    RESOLVED_ALERT_STATUS,
    reconcile_resolutions,
)

from .conftest import FakeAlertsTable, RecordingPublisher

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# Constant inputs shared across the cases; kept at module level (PYQUALITY-05).
PROPERTY_ID: str = "ALOHA-CHI-001"
ALERTS_TABLE_NAME: str = "pulse-alerts"
FIXED_NOW: str = "2025-01-15T00:00:00+00:00"


def _open_forecast_alert(dedupe_key: str) -> dict[str, Any]:
    """Build a minimal open FORECAST_OVERSELL alert item for a dedupeKey.

    Mirrors the identity attributes the resolve path reads (``alertId``,
    ``dedupeKey``, ``status``, ``type``); the alertId is derived from the key so
    each distinct key yields a distinct, identifiable record.

    Args:
        dedupe_key: The deterministic dedupeKey the open alert carries.

    Returns:
        A camelCase open predictive alert item.
    """
    return {
        "alertId": f"alert-{dedupe_key}",
        "propertyId": PROPERTY_ID,
        "type": FORECAST_ALERT_TYPE,
        "status": OPEN_ALERT_STATUS,
        "dedupeKey": dedupe_key,
    }


def _table_getter_for(table: FakeAlertsTable) -> Any:
    """Return a table-getter seam that always yields ``table``."""

    def _getter(_table_name: str) -> FakeAlertsTable:
        return table

    return _getter


# ---------------------------------------------------------------------------
# Property 21: No-longer-forecast open alerts resolve exactly once
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 21: No-longer-forecast open alerts resolve exactly once
@PROPERTY_SETTINGS
@given(
    # A set of distinct dedupeKeys for the open alerts, plus an index selecting
    # how many of them (a subset) are still predicted this run.
    dedupe_keys=st.lists(
        st.text(min_size=1, max_size=12).map(lambda s: f"forecast#p#OVERSELL#{s}"),
        min_size=1,
        max_size=8,
        unique=True,
    ),
    subset_size=st.integers(min_value=0, max_value=8),
)
def test_property_21_stale_open_alerts_resolve_exactly_once(
    dedupe_keys: list[str], subset_size: int
) -> None:
    """Open forecast alerts not still predicted resolve exactly once; rest kept.

    For a set of open FORECAST_OVERSELL alerts (distinct dedupeKeys) and a
    predicted set that is a SUBSET of those keys, asserts:
    - every open alert whose dedupeKey is NOT predicted is resolved with exactly
      one conditional UpdateItem setting status -> RESOLVED, guarded on
      still-open;
    - the returned resolved list is exactly those stale alerts;
    - open alerts whose dedupeKey IS predicted get no update and are not returned.
    """
    open_alerts = [_open_forecast_alert(key) for key in dedupe_keys]
    # The predicted set is a subset of the open keys; the complement is stale.
    predicted_keys = set(dedupe_keys[: min(subset_size, len(dedupe_keys))])
    stale_keys = set(dedupe_keys) - predicted_keys

    table = FakeAlertsTable(query_items=open_alerts)
    publisher = RecordingPublisher()

    resolved = reconcile_resolutions(
        predicted_keys,
        property_id=PROPERTY_ID,
        alerts_table_name=ALERTS_TABLE_NAME,
        now=FIXED_NOW,
        table_getter=_table_getter_for(table),
        realtime_publisher=publisher,
    )

    # Exactly one update per stale alert, and none for still-predicted alerts.
    assert len(table.updates) == len(stale_keys)
    updated_alert_ids = {update["Key"]["alertId"] for update in table.updates}
    assert updated_alert_ids == {f"alert-{key}" for key in stale_keys}

    # Each update transitions to RESOLVED and is guarded on still-open. The
    # guard is a ConditionExpression comparing status to the open value, which
    # is what makes the RESOLVED transition happen exactly once per alert.
    for update in table.updates:
        assert update["ExpressionAttributeValues"][":resolved"] == RESOLVED_ALERT_STATUS
        condition = update["ConditionExpression"]
        assert OPEN_ALERT_STATUS in condition.get_expression()["values"]

    # The returned resolved list is exactly the stale alerts, now RESOLVED.
    resolved_keys = {item["dedupeKey"] for item in resolved}
    assert resolved_keys == stale_keys
    assert all(item["status"] == RESOLVED_ALERT_STATUS for item in resolved)

    # Still-predicted alerts are neither updated nor returned.
    for predicted_key in predicted_keys:
        assert f"alert-{predicted_key}" not in updated_alert_ids
        assert predicted_key not in resolved_keys


def test_conditional_check_failure_is_skipped_not_double_resolved() -> None:
    """A conditional-check failure (already terminal) is skipped, never raised.

    When the table's ``update_item`` raises ConditionalCheckFailedException
    (simulating an alert already resolved by a prior run or a concurrent race),
    ``reconcile_resolutions`` treats the alert as already terminal: it does not
    double-resolve, does not raise, and excludes the alert from the returned
    list, so the RESOLVED transition happens exactly once (Req 7.5).
    """
    stale_key = "forecast#p#OVERSELL#2025-02-01"
    open_alerts = [_open_forecast_alert(stale_key)]

    # raise_conditional=True: the guarded update raises, simulating "already
    # terminal" so reconcile should skip it silently.
    table = FakeAlertsTable(query_items=open_alerts, raise_conditional=True)
    publisher = RecordingPublisher()

    # No predicted keys -> the single open alert is stale; but its update fails
    # the conditional guard and must be skipped without error.
    resolved = reconcile_resolutions(
        set(),
        property_id=PROPERTY_ID,
        alerts_table_name=ALERTS_TABLE_NAME,
        now=FIXED_NOW,
        table_getter=_table_getter_for(table),
        realtime_publisher=publisher,
    )

    # Skipped: no successful update recorded, and the alert is not returned.
    assert table.updates == []
    assert resolved == []
