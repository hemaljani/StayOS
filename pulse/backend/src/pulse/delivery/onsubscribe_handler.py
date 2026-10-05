"""AppSync Events OnSubscribe/OnPublish authorization logic (Component 4a).

The AppSync Events realtime API scopes subscriptions server-side so a client can
never receive events for a property it is not associated with, nor an escalation
nudge targeted at another manager (Requirement 16.6, 6.1; Property 28). This
module holds the **pure** subscription-authorization decision
(:func:`authorize_subscription`) and the delivered-event projection
(:func:`delivered_events`) that back Property 28, plus thin Lambda handlers
(:func:`on_subscribe_handler`, :func:`on_publish_handler`) that AppSync can
invoke as a Lambda-backed namespace handler.

Channel model (namespace ``pulse``):
    * ``/pulse/alerts/{propertyId}``            property broadcast (feed).
    * ``/pulse/alerts/*``                        multi-property wildcard, narrowed
      by the handler to the caller's associated-property set.
    * ``/pulse/alerts/{propertyId}/{gmAlias}``   per-user unicast escalation nudge.
    * ``/pulse/alerts/{propertyId}/detail/{id}`` optional per-alert detail channel.

Authorization rules (Property 28):
    * A **property-channel** subscription is accepted iff the requested property
      is in the caller's associated-property set.
    * A **wildcard** subscription is accepted and narrowed to exactly the
      caller's associated-property set (never wider).
    * A **per-user channel** subscription is accepted iff the property is
      associated with the caller **and** the ``{gmAlias}`` equals the caller's
      own identity -- a user may subscribe only to their own per-user channel.
    * Any other channel shape is rejected.

Note:
    The authoritative runtime enforcement is the AppSync Events namespace
    handler (authored as APPSYNC_JS code on the ``pulse`` ChannelNamespace in
    ``pulse-api.yaml``). This Python module mirrors that logic exactly so the
    property-scope contract is property-tested here (Property 28); if PULSE
    later switches the namespace to a Lambda-backed handler, these functions are
    the handler.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pulse.api.identity import CallerIdentity, identity_from_claims
from pulse.common.logging import get_logger
from pulse.delivery.realtime_publish import NAMESPACE

logger = get_logger("pulse-onsubscribe")

# The fixed channel prefix segments under the ``pulse`` namespace.
_ALERTS_PREFIX = (NAMESPACE, "alerts")

# The reserved segment that distinguishes the optional per-alert detail channel
# from a per-user (gmAlias) channel.
_DETAIL_SEGMENT = "detail"

# The wildcard token accepted in place of a property id.
_WILDCARD = "*"


@dataclass(frozen=True)
class SubscribeDecision:
    """The pure outcome of authorizing a subscription request.

    Attributes:
        allowed: Whether the subscription is accepted.
        reason: A short machine-friendly reason (accept or reject cause).
        authorized_properties: The properties the accepted subscription is
            scoped to (a single property for a property channel, the caller's
            whole set for a narrowed wildcard, empty when rejected).
    """

    allowed: bool
    reason: str
    authorized_properties: frozenset[str] = field(default_factory=frozenset)


def _channel_segments(channel: str) -> list[str]:
    """Split a channel path into non-empty segments.

    Args:
        channel: The channel path (e.g. ``/pulse/alerts/ALOHA-CHI-001``).

    Returns:
        The non-empty path segments.
    """
    return [segment for segment in channel.split("/") if segment]


def authorize_subscription(
    channel: str, identity: CallerIdentity
) -> SubscribeDecision:
    """Authorize a single channel subscription for a caller (pure, Property 28).

    Args:
        channel: The requested channel path under the ``pulse`` namespace.
        identity: The authenticated caller (associated properties + identity).

    Returns:
        A :class:`SubscribeDecision` with the accept/reject outcome and, when
        accepted, the properties the subscription is scoped to.
    """
    segments = _channel_segments(channel)
    if tuple(segments[:2]) != _ALERTS_PREFIX:
        return SubscribeDecision(False, "unknown-namespace")
    rest = segments[2:]

    # /pulse/alerts/{propertyId}  or  /pulse/alerts/*
    if len(rest) == 1:
        target = rest[0]
        if target == _WILDCARD:
            # Wildcard is accepted but narrowed to the caller's own set: they
            # will only ever receive events for their associated properties.
            return SubscribeDecision(
                True, "wildcard-narrowed", identity.properties
            )
        if identity.is_associated_with(target):
            return SubscribeDecision(True, "property-associated", frozenset({target}))
        return SubscribeDecision(False, "property-not-associated")

    # /pulse/alerts/{propertyId}/{gmAlias}  (per-user unicast)
    if len(rest) == 2:
        property_id, gm_alias = rest
        associated = identity.is_associated_with(property_id)
        own_identity = gm_alias == identity.gm_alias
        if associated and own_identity:
            return SubscribeDecision(
                True, "own-per-user-channel", frozenset({property_id})
            )
        if not associated:
            return SubscribeDecision(False, "property-not-associated")
        return SubscribeDecision(False, "not-own-per-user-channel")

    # /pulse/alerts/{propertyId}/detail/{alertId}  (optional per-alert channel)
    if len(rest) == 3 and rest[1] == _DETAIL_SEGMENT:
        property_id = rest[0]
        if identity.is_associated_with(property_id):
            return SubscribeDecision(
                True, "detail-channel", frozenset({property_id})
            )
        return SubscribeDecision(False, "property-not-associated")

    return SubscribeDecision(False, "unrecognized-channel")


def delivered_events(
    identity: CallerIdentity,
    subscription_requests: Sequence[str],
    published_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the events a caller actually receives (pure, Property 28).

    Given the channels the caller *requested* and the events published across
    all properties, this returns exactly the events the caller receives after
    server-side authorization: broadcast feed events only for associated
    properties, and per-user escalation events only when the caller is the
    targeted recipient on an associated property. Requests to unauthorized
    channels contribute nothing.

    An event is expected to carry ``propertyId`` and, when it is a per-user
    escalation nudge, a ``targetGmAlias`` naming the intended recipient.

    Args:
        identity: The authenticated caller.
        subscription_requests: The channel paths the caller asked to subscribe.
        published_events: The events published across all properties.

    Returns:
        The events the caller receives, de-duplicated by ``alertId`` +
        ``eventType`` + ``targetGmAlias`` while preserving first-seen order.
    """
    # Resolve which properties the caller is authorized to receive broadcasts
    # for, and whether they hold their own per-user (unicast) subscription.
    broadcast_properties: set[str] = set()
    unicast_properties: set[str] = set()
    for channel in subscription_requests:
        decision = authorize_subscription(channel, identity)
        if not decision.allowed:
            continue
        segments = _channel_segments(channel)
        rest = segments[2:]
        if len(rest) == 2 and rest[1] != _DETAIL_SEGMENT:
            # Own per-user channel: unicast escalation events for this property.
            unicast_properties.update(decision.authorized_properties)
        else:
            # Property broadcast / wildcard / detail: feed events.
            broadcast_properties.update(decision.authorized_properties)

    received: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for event in published_events:
        property_id = event.get("propertyId")
        target = event.get("targetGmAlias")
        if target:
            # Unicast escalation nudge: delivered only to the targeted recipient
            # on a property they hold their own per-user subscription for.
            if property_id in unicast_properties and target == identity.gm_alias:
                _append_unique(received, seen, event)
        else:
            # Broadcast feed event: delivered for associated properties only.
            if property_id in broadcast_properties:
                _append_unique(received, seen, event)
    return received


def _append_unique(
    received: list[dict[str, Any]],
    seen: set[tuple[Any, Any, Any]],
    event: Mapping[str, Any],
) -> None:
    """Append an event to the received list once, de-duplicated by identity.

    Args:
        received: The accumulating received-events list.
        seen: The set of already-seen event identity tuples.
        event: The event to consider.
    """
    key = (event.get("alertId"), event.get("eventType"), event.get("targetGmAlias"))
    if key in seen:
        return
    seen.add(key)
    received.append(dict(event))


# ---------------------------------------------------------------------------
# Thin Lambda handlers (used when the namespace is Lambda-backed)
# ---------------------------------------------------------------------------


def _identity_from_event(event: Mapping[str, Any]) -> CallerIdentity:
    """Extract the caller identity from an AppSync Events invocation event.

    AppSync places the authenticated Cognito identity under ``identity``; its
    ``claims`` mapping (or the identity object itself) carries the username and
    custom property claims.

    Args:
        event: The AppSync Events invocation event.

    Returns:
        The caller identity derived from the event's claims.
    """
    identity_ctx = event.get("identity") or {}
    if isinstance(identity_ctx, Mapping):
        claims = identity_ctx.get("claims")
        if isinstance(claims, Mapping):
            return identity_from_claims(claims)
        return identity_from_claims(identity_ctx)
    return identity_from_claims({})


def _event_channel(event: Mapping[str, Any]) -> str:
    """Resolve the channel path from an AppSync Events invocation event.

    Args:
        event: The AppSync Events invocation event.

    Returns:
        The channel path, or an empty string when absent.
    """
    info = event.get("info")
    if isinstance(info, Mapping) and info.get("channel"):
        return str(info["channel"])
    if event.get("channel"):
        return str(event["channel"])
    return ""


def on_subscribe_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AppSync Events OnSubscribe Lambda handler (thin dispatcher).

    Delegates the accept/reject decision to :func:`authorize_subscription` and
    returns an authorization result. A rejected subscription returns
    ``{"allow": False, ...}`` so the resolver denies it; an accepted one returns
    the properties the subscription is scoped to.

    Args:
        event: The AppSync Events OnSubscribe invocation event.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A decision dict (``allow``, ``reason``, ``authorizedProperties``).
    """
    identity = _identity_from_event(event)
    channel = _event_channel(event)
    decision = authorize_subscription(channel, identity)
    logger.info(
        "OnSubscribe decision",
        extra={
            "gmAlias": identity.gm_alias,
            "channel": channel,
            "allowed": decision.allowed,
            "reason": decision.reason,
        },
    )
    return {
        "allow": decision.allowed,
        "reason": decision.reason,
        "authorizedProperties": sorted(decision.authorized_properties),
    }


def on_publish_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AppSync Events OnPublish Lambda handler (event normalizer).

    Validates and normalizes the published event shape before fan-out; it is
    **not** used for authorization (publishing is a signed backend call). Events
    missing an ``alertId`` or ``propertyId`` are dropped so malformed payloads
    never reach subscribers.

    Args:
        event: The AppSync Events OnPublish invocation event carrying an
            ``events`` list.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A dict with the normalized ``events`` list.
    """
    raw_events = event.get("events") or []
    normalized: list[dict[str, Any]] = []
    for entry in raw_events:
        payload = entry.get("payload") if isinstance(entry, Mapping) else None
        payload = payload if isinstance(payload, Mapping) else entry
        if not isinstance(payload, Mapping):
            continue
        if payload.get("alertId") and payload.get("propertyId"):
            normalized.append(dict(payload))
        else:
            logger.warning(
                "Dropping malformed realtime event on publish",
                extra={"keys": sorted(payload.keys())},
            )
    return {"events": normalized}


__all__ = [
    "SubscribeDecision",
    "authorize_subscription",
    "delivered_events",
    "on_subscribe_handler",
    "on_publish_handler",
]
