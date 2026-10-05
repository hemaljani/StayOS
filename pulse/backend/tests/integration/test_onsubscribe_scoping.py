"""OnSubscribe property-scoping integration test (Task 23.3).

Exercises the AppSync Events subscription-authorization logic
(:mod:`pulse.delivery.onsubscribe_handler`) directly - the Python mirror of the
APPSYNC_JS namespace handler documented in ``pulse-api.yaml`` - to prove the
server-side scoping contract (Requirements 16.6, 6.1; Property 28):

    * a client associated with property A is rejected on property B's channel
      and receives none of B's events,
    * a client is rejected on ANOTHER user's per-user channel
      ``/pulse/alerts/{propertyId}/{otherGmAlias}``,
    * an escalation-targeted per-user event is delivered to the targeted
      recipient's own channel but a non-targeted user on the same property does
      not receive it.

Both the accept/reject decision and the delivered-event routing (unicast vs
broadcast) are asserted, with no AWS dependency.
"""

from __future__ import annotations

from pulse.api.identity import CallerIdentity
from pulse.delivery import onsubscribe_handler as osh

PROPERTY_A = "ALOHA-CHI-001"
PROPERTY_B = "ALOHA-MKE-002"


def _identity(gm_alias: str, properties: set[str]) -> CallerIdentity:
    """Build a caller identity scoped to a set of properties."""
    return CallerIdentity(gm_alias=gm_alias, properties=frozenset(properties))


def test_client_rejected_on_other_property_channel_and_receives_no_events() -> None:
    """A property-A client is rejected on property B and gets none of B's events.

    Validates: Requirement 16.6; Property 28
    """
    identity = _identity("jsmith", {PROPERTY_A})

    # Accepted on its own property, rejected on the other property's channel.
    own = osh.authorize_subscription(f"/pulse/alerts/{PROPERTY_A}", identity)
    other = osh.authorize_subscription(f"/pulse/alerts/{PROPERTY_B}", identity)
    assert own.allowed is True
    assert other.allowed is False
    assert other.reason == "property-not-associated"

    # Even if the client "requests" B's channel, no B events are delivered.
    requests = [f"/pulse/alerts/{PROPERTY_A}", f"/pulse/alerts/{PROPERTY_B}"]
    published = [
        {"alertId": "a-A", "eventType": "ALERT_CREATED", "propertyId": PROPERTY_A},
        {"alertId": "a-B", "eventType": "ALERT_CREATED", "propertyId": PROPERTY_B},
    ]
    received = osh.delivered_events(identity, requests, published)
    received_ids = {event["alertId"] for event in received}
    assert received_ids == {"a-A"}


def test_client_rejected_on_another_users_per_user_channel() -> None:
    """A client cannot subscribe to another manager's per-user channel.

    Validates: Requirements 16.6, 6.1; Property 28
    """
    identity = _identity("jsmith", {PROPERTY_A})

    own_channel = osh.authorize_subscription(
        f"/pulse/alerts/{PROPERTY_A}/jsmith", identity
    )
    other_user_channel = osh.authorize_subscription(
        f"/pulse/alerts/{PROPERTY_A}/rmoore", identity
    )

    assert own_channel.allowed is True
    assert own_channel.reason == "own-per-user-channel"
    assert other_user_channel.allowed is False
    assert other_user_channel.reason == "not-own-per-user-channel"


def test_escalation_nudge_reaches_target_only_not_other_user_same_property() -> None:
    """A per-user escalation nudge reaches its target, not a co-located peer.

    Validates: Requirements 16.6, 6.1; Property 28
    """
    target = _identity("rmoore", {PROPERTY_A})
    bystander = _identity("jsmith", {PROPERTY_A})

    # A targeted escalation nudge for rmoore on property A.
    nudge = {
        "alertId": "esc-1",
        "eventType": "ALERT_UPDATED",
        "propertyId": PROPERTY_A,
        "targetGmAlias": "rmoore",
    }
    # A broadcast feed event both users on property A should see.
    broadcast = {
        "alertId": "feed-1",
        "eventType": "ALERT_CREATED",
        "propertyId": PROPERTY_A,
    }
    published = [broadcast, nudge]

    # Each user subscribes to the property feed and their own per-user channel.
    target_requests = [
        f"/pulse/alerts/{PROPERTY_A}",
        f"/pulse/alerts/{PROPERTY_A}/rmoore",
    ]
    bystander_requests = [
        f"/pulse/alerts/{PROPERTY_A}",
        f"/pulse/alerts/{PROPERTY_A}/jsmith",
    ]

    target_received = {
        event["alertId"]
        for event in osh.delivered_events(target, target_requests, published)
    }
    bystander_received = {
        event["alertId"]
        for event in osh.delivered_events(bystander, bystander_requests, published)
    }

    # The target receives both the broadcast and its own nudge.
    assert target_received == {"feed-1", "esc-1"}
    # The bystander receives the broadcast but NOT the other user's nudge.
    assert bystander_received == {"feed-1"}


def test_on_subscribe_handler_end_to_end_decisions() -> None:
    """The thin OnSubscribe handler returns the same accept/reject decisions.

    Validates: Requirements 16.6, 6.1; Property 28 (handler wiring)
    """
    claims = {"cognito:username": "jsmith", "properties": f"{PROPERTY_A}"}

    accept = osh.on_subscribe_handler(
        {
            "identity": {"claims": claims},
            "info": {"channel": f"/pulse/alerts/{PROPERTY_A}"},
        },
        None,
    )
    reject_property = osh.on_subscribe_handler(
        {
            "identity": {"claims": claims},
            "info": {"channel": f"/pulse/alerts/{PROPERTY_B}"},
        },
        None,
    )
    reject_other_user = osh.on_subscribe_handler(
        {
            "identity": {"claims": claims},
            "info": {"channel": f"/pulse/alerts/{PROPERTY_A}/rmoore"},
        },
        None,
    )

    assert accept["allow"] is True
    assert reject_property["allow"] is False
    assert reject_other_user["allow"] is False
    assert reject_other_user["reason"] == "not-own-per-user-channel"
