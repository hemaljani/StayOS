"""Property and unit tests for property-scoped alert reads (Requirement 16.6).

Covers Property 25 (alerts are scoped to the user's properties) as a pure
property over :func:`scope_alerts`, plus moto-backed unit tests for
:func:`list_alerts` and :func:`get_alert` confirming cross-property isolation
end-to-end.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from pulse.api import alerts_repository as repo
from tests.api.conftest import (
    ALERTS_TABLE_NAME,
    create_alerts_table,
    identity,
    make_alert_item,
    table_getter,
)

PROPERTY_SETTINGS = settings(max_examples=200)

# A universe of property ids the generators draw from.
_PROPERTY_UNIVERSE = ["P-A", "P-B", "P-C", "P-D", "P-E"]


# ---------------------------------------------------------------------------
# Property 25: alerts are scoped to the user's properties
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 25: Alerts are scoped to the user's
# properties
@PROPERTY_SETTINGS
@given(
    allowed=st.sets(st.sampled_from(_PROPERTY_UNIVERSE)),
    alert_properties=st.lists(st.sampled_from(_PROPERTY_UNIVERSE), max_size=30),
)
def test_property_25_scope_alerts_only_returns_allowed(
    allowed: set[str], alert_properties: list[str]
) -> None:
    """Scoping returns exactly the alerts whose property the caller owns.

    Validates: Requirements 16.6
    """
    alerts = [
        {"alertId": f"alert-{i}", "propertyId": prop}
        for i, prop in enumerate(alert_properties)
    ]
    scoped = repo.scope_alerts(alerts, frozenset(allowed))

    # No alert outside the caller's associated set is ever returned.
    assert all(alert["propertyId"] in allowed for alert in scoped)
    # Every in-scope alert is retained (nothing entitled is dropped).
    expected_ids = {a["alertId"] for a in alerts if a["propertyId"] in allowed}
    assert {a["alertId"] for a in scoped} == expected_ids


@PROPERTY_SETTINGS
@given(
    requested=st.one_of(st.none(), st.sampled_from(_PROPERTY_UNIVERSE)),
    allowed=st.sets(st.sampled_from(_PROPERTY_UNIVERSE)),
)
def test_property_25_target_properties_subset_of_allowed(
    requested: str | None, allowed: set[str]
) -> None:
    """The queried property set is always a subset of the caller's set.

    Validates: Requirements 16.6
    """
    caller = identity("jsmith", allowed)
    targets = repo.select_target_properties(requested, caller)
    assert set(targets).issubset(allowed)
    if requested is not None and requested not in allowed:
        # A request for a non-associated property targets nothing.
        assert targets == []


# ---------------------------------------------------------------------------
# Unit tests: list_alerts / get_alert end-to-end over moto
# ---------------------------------------------------------------------------


def test_list_alerts_scopes_and_filters() -> None:
    """The feed returns only associated-property alerts, honoring filters.

    Validates: Requirements 15.4, 15.5, 16.6
    """
    with mock_aws():
        table = create_alerts_table()
        table.put_item(
            Item=make_alert_item("a1", "P-A", tier="CRITICAL", status="UNACKNOWLEDGED")
        )
        table.put_item(
            Item=make_alert_item("a2", "P-A", tier="WARNING", status="UNACKNOWLEDGED")
        )
        table.put_item(
            Item=make_alert_item("b1", "P-B", tier="CRITICAL", status="UNACKNOWLEDGED")
        )
        caller = identity("jsmith", {"P-A"})

        all_a = repo.list_alerts(
            caller,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
        )
        critical_a = repo.list_alerts(
            caller,
            tier="CRITICAL",
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
        )

    returned_ids = {a["alertId"] for a in all_a}
    assert returned_ids == {"a1", "a2"}  # no P-B leak
    assert {a["alertId"] for a in critical_a} == {"a1"}


def test_get_alert_denies_out_of_scope() -> None:
    """A GM cannot fetch an alert for a property they are not associated with.

    Validates: Requirements 15.7, 16.6
    """
    with mock_aws():
        table = create_alerts_table()
        table.put_item(Item=make_alert_item("b1", "P-B"))
        caller = identity("jsmith", {"P-A"})

        in_scope = repo.get_alert(
            caller, "b1", alerts_table_name=ALERTS_TABLE_NAME, table_getter=table_getter
        )
        missing = repo.get_alert(
            caller,
            "nope",
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
        )

    # Out-of-scope and missing are both indistinguishable None.
    assert in_scope is None
    assert missing is None
