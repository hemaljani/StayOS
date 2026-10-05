"""Property and unit tests for AppSync Events OnSubscribe/OnPublish logic.

Covers Property 28: a subscriber is authorized for a property channel only when
associated with the property, and for a per-user channel only when it is their
own channel; delivered events never include a non-associated property's events
nor another user's targeted escalation nudges.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.api.identity import CallerIdentity
from pulse.delivery import onsubscribe_handler as osh

PROPERTY_SETTINGS = settings(max_examples=200)

_PROPERTY_UNIVERSE = ["P-A", "P-B", "P-C", "P-D"]
_USER_UNIVERSE = ["jsmith", "rmoore", "twalsh"]


def _identity(gm_alias: str, properties: set[str]) -> CallerIdentity:
    """Build a caller identity for the tests."""
    return CallerIdentity(gm_alias=gm_alias, properties=frozenset(properties))


# ---------------------------------------------------------------------------
# Property 28: subscription authorization (property scope + own-identity)
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 28: Realtime subscribers only receive
# events for their associated properties, and only their own per-user channel
@PROPERTY_SETTINGS
@given(
    associated=st.sets(st.sampled_from(_PROPERTY_UNIVERSE)),
    target_property=st.sampled_from(_PROPERTY_UNIVERSE),
    channel_owner=st.sampled_from(_USER_UNIVERSE),
    caller=st.sampled_from(_USER_UNIVERSE),
)
def test_property_28_subscription_authorization(
    associated: set[str],
    target_property: str,
    channel_owner: str,
    caller: str,
) -> None:
    """Property channels require association; per-user channels require identity.

    Validates: Requirements 16.6, 6.1
    """
    identity = _identity(caller, associated)

    # Property broadcast channel: accepted iff the property is associated.
    property_channel = f"/pulse/alerts/{target_property}"
    property_decision = osh.authorize_subscription(property_channel, identity)
    assert property_decision.allowed is (target_property in associated)

    # Per-user channel: accepted iff associated AND the channel is the caller's.
    per_user_channel = f"/pulse/alerts/{target_property}/{channel_owner}"
    per_user_decision = osh.authorize_subscription(per_user_channel, identity)
    expected = (target_property in associated) and (channel_owner == caller)
    assert per_user_decision.allowed is expected


@PROPERTY_SETTINGS
@given(associated=st.sets(st.sampled_from(_PROPERTY_UNIVERSE), min_size=1))
def test_property_28_wildcard_narrows_to_associated_set(
    associated: set[str],
) -> None:
    """A wildcard subscription is narrowed to exactly the caller's set.

    Validates: Requirements 16.6
    """
    identity = _identity("jsmith", associated)
    decision = osh.authorize_subscription("/pulse/alerts/*", identity)
    assert decision.allowed is True
    assert decision.authorized_properties == frozenset(associated)


# ---------------------------------------------------------------------------
# Property 28: delivered-event projection
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 28: Realtime subscribers only receive
# events for their associated properties, and only their own per-user channel
@PROPERTY_SETTINGS
@given(
    associated=st.sets(st.sampled_from(_PROPERTY_UNIVERSE), min_size=1),
    caller=st.sampled_from(_USER_UNIVERSE),
    event_specs=st.lists(
        st.tuples(
            st.sampled_from(_PROPERTY_UNIVERSE),
            st.one_of(st.none(), st.sampled_from(_USER_UNIVERSE)),
        ),
        max_size=20,
    ),
)
def test_property_28_delivered_events_respect_scope(
    associated: set[str],
    caller: str,
    event_specs: list[tuple[str, str | None]],
) -> None:
    """A caller receives only associated-property feed + own targeted nudges.

    Validates: Requirements 16.6, 6.1
    """
    identity = _identity(caller, associated)
    # The caller subscribes to the wildcard feed and its own per-user channel on
    # every associated property.
    requests = ["/pulse/alerts/*"] + [
        f"/pulse/alerts/{prop}/{caller}" for prop in associated
    ]
    events = [
        {
            "alertId": f"alert-{i}",
            "eventType": "ALERT_UPDATED",
            "propertyId": prop,
            "targetGmAlias": target,
        }
        for i, (prop, target) in enumerate(event_specs)
    ]

    received = osh.delivered_events(identity, requests, events)
    received_ids = {event["alertId"] for event in received}

    for i, (prop, target) in enumerate(event_specs):
        alert_id = f"alert-{i}"
        if target is None:
            # Broadcast feed event: received iff the property is associated.
            assert (alert_id in received_ids) is (prop in associated)
        else:
            # Unicast nudge: received iff associated AND targeted at the caller.
            expected = (prop in associated) and (target == caller)
            assert (alert_id in received_ids) is expected


# ---------------------------------------------------------------------------
# Unit tests: thin handlers
# ---------------------------------------------------------------------------


def test_on_subscribe_handler_allows_own_channel() -> None:
    """The OnSubscribe handler allows a caller's own per-user channel."""
    event = {
        "identity": {"claims": {"cognito:username": "jsmith", "properties": "P-A,P-B"}},
        "info": {"channel": "/pulse/alerts/P-A/jsmith"},
    }
    result = osh.on_subscribe_handler(event, None)
    assert result["allow"] is True
    assert "P-A" in result["authorizedProperties"]


def test_on_subscribe_handler_rejects_other_users_channel() -> None:
    """The OnSubscribe handler rejects another user's per-user channel."""
    event = {
        "identity": {"claims": {"cognito:username": "jsmith", "properties": "P-A"}},
        "info": {"channel": "/pulse/alerts/P-A/rmoore"},
    }
    result = osh.on_subscribe_handler(event, None)
    assert result["allow"] is False
    assert result["reason"] == "not-own-per-user-channel"


def test_on_publish_handler_drops_malformed_events() -> None:
    """The OnPublish normalizer keeps well-formed events and drops the rest."""
    event = {
        "events": [
            {"payload": {"alertId": "a1", "propertyId": "P-A", "eventType": "X"}},
            {"payload": {"eventType": "missing-ids"}},
        ]
    }
    result = osh.on_publish_handler(event, None)
    assert len(result["events"]) == 1
    assert result["events"][0]["alertId"] == "a1"
