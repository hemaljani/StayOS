"""Foreground realtime publishing over AWS AppSync Events (Component 4a).

This module is the single place that knows how to push an alert event onto an
AppSync Events channel so that any *open* PWA client updates its live feed
instantly. It is imported by the delivery layer (``pulse-push-service``), the
Escalation Service, the Action Executor, and the INFO batcher, so the AppSync
Events publish contract lives in exactly one place (design Decision: "one
Lambda, two channels" -- the realtime seam is shared).

Channels (under the ``pulse`` namespace):
    * **Broadcast** ``/pulse/alerts/{propertyId}`` -- alert create / acknowledge
      / resolve feed events, delivered to every client subscribed to the
      property.
    * **Unicast** ``/pulse/alerts/{propertyId}/{gmAlias}`` -- escalation-targeted
      nudges to a single recipient's device.

Design guarantees enforced here:
    * **Full-enough event payload.** Each event carries enough of the alert
      (``eventType``, ``alertId``, ``propertyId``, ``tier``, ``type``,
      ``status``, ``title``, ``escalationStatus``, ``hasTriageBrief``,
      ``lastStatusChangeAt``) that the client updates the card without a REST
      round-trip. The heavy ``triageBrief`` is intentionally omitted.
    * **Batch limit.** AppSync Events accepts at most 5 events per publish call,
      so :func:`publish` chunks any larger fan-out into successive batches of 5.
    * **Best-effort, non-blocking.** A publish failure never propagates to the
      caller: the originating operation (alert persist, status change, committed
      RESOLVED transaction) must not fail because realtime delivery hiccuped.
      Failures are logged with the ``alertId`` and channel; open clients
      reconcile via ``GET /alerts`` on reconnect.

The signing of the HTTP publish is kept behind the injectable
:data:`PublisherFn` seam so tests inject a fake and never sign or make a network
call. The default publisher (:class:`SigV4EventPublisher`) SigV4-signs a POST to
the AppSync Events HTTP endpoint (service ``appsync``) read from the
``REALTIME_HTTP_ENDPOINT`` environment variable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional

from pulse.common.config import ENV_AWS_REGION, get_optional_env
from pulse.common.logging import get_logger

logger = get_logger("pulse-push-service")

# Environment variables for the AppSync Events HTTP publish endpoint (never
# hardcoded; PYQUALITY-06 / NAMING-03). The client fetches the same endpoint via
# GET /config/realtime for its WebSocket subscription.
ENV_REALTIME_HTTP_ENDPOINT = "REALTIME_HTTP_ENDPOINT"

# The ``pulse`` channel namespace and channel-path builders. A single namespace
# for the whole feature (design Component 4a).
NAMESPACE = "pulse"

# AppSync Events publishes at most 5 events per HTTP publish call.
MAX_EVENTS_PER_PUBLISH = 5

# The three event types a client acts on to drive the live feed.
EVENT_ALERT_CREATED = "ALERT_CREATED"
EVENT_ALERT_UPDATED = "ALERT_UPDATED"
EVENT_ALERT_RESOLVED = "ALERT_RESOLVED"

# A publisher takes a channel path and a batch of <= 5 event dicts and performs
# the HTTP publish, raising on failure. Injectable so tests never sign/POST.
PublisherFn = Callable[[str, Sequence[Mapping[str, Any]]], None]


def broadcast_channel(property_id: str) -> str:
    """Return the property broadcast channel path.

    Args:
        property_id: The property the alert belongs to.

    Returns:
        The channel path ``/pulse/alerts/{propertyId}``.
    """
    return f"/{NAMESPACE}/alerts/{property_id}"


def unicast_channel(property_id: str, gm_alias: str) -> str:
    """Return the per-user (escalation-targeted) unicast channel path.

    Args:
        property_id: The property the alert belongs to.
        gm_alias: The specific recipient the nudge is targeted at.

    Returns:
        The channel path ``/pulse/alerts/{propertyId}/{gmAlias}``.
    """
    return f"/{NAMESPACE}/alerts/{property_id}/{gm_alias}"


def build_event(
    event_type: str,
    *,
    alert_id: str,
    property_id: str,
    tier: Optional[str] = None,
    alert_type: Optional[str] = None,
    status: Optional[str] = None,
    title: Optional[str] = None,
    escalation_status: Optional[str] = None,
    has_triage_brief: bool = False,
    last_status_change_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build a full-enough realtime event payload for an alert.

    The payload deliberately omits the heavy ``triageBrief`` so the event stays
    small; the client fetches the full brief via ``GET /alerts/{alertId}`` when
    the GM opens the card (design Component 4a).

    Args:
        event_type: One of ``ALERT_CREATED``, ``ALERT_UPDATED``,
            ``ALERT_RESOLVED``.
        alert_id: The alert identifier.
        property_id: The owning property.
        tier: The alert tier (``CRITICAL``/``WARNING``/``INFO``).
        alert_type: The alert type (e.g. ``WALK_RISK``); serialized as ``type``.
        status: The alert lifecycle status.
        title: The alert title.
        escalation_status: The mandatory-review indicator, if any.
        has_triage_brief: Whether the alert carries a triage brief.
        last_status_change_at: ISO 8601 timestamp of the last status change.

    Returns:
        A JSON-serializable event dict with camelCase keys (NAMING-05).
    """
    return {
        "eventType": event_type,
        "alertId": alert_id,
        "propertyId": property_id,
        "tier": tier,
        "type": alert_type,
        "status": status,
        "title": title,
        "escalationStatus": escalation_status,
        "hasTriageBrief": has_triage_brief,
        "lastStatusChangeAt": last_status_change_at,
    }


def event_from_item(
    item: Mapping[str, Any], event_type: str
) -> dict[str, Any]:
    """Build a realtime event from a ``pulse-alerts`` item.

    Args:
        item: A ``pulse-alerts`` DynamoDB item (camelCase attributes).
        event_type: The event type to stamp on the payload.

    Returns:
        A full-enough event dict for :func:`publish`.
    """
    return build_event(
        event_type,
        alert_id=item["alertId"],
        property_id=item["propertyId"],
        tier=item.get("tier"),
        alert_type=item.get("type"),
        status=item.get("status"),
        title=item.get("title"),
        escalation_status=item.get("escalationStatus"),
        has_triage_brief=bool(item.get("triageBrief")),
        last_status_change_at=item.get("lastStatusChangeAt"),
    )


def chunk_events(
    events: Sequence[Mapping[str, Any]],
    size: int = MAX_EVENTS_PER_PUBLISH,
) -> list[list[Mapping[str, Any]]]:
    """Split events into successive batches no larger than ``size``.

    Preserves order and never drops or duplicates an event, so the union of the
    returned batches equals the input (AppSync Events allows at most 5 events
    per publish call).

    Args:
        events: The events to chunk.
        size: The maximum batch size (default 5).

    Returns:
        A list of batches, each with at most ``size`` events.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [list(events[i : i + size]) for i in range(0, len(events), size)]


# ---------------------------------------------------------------------------
# Default SigV4 HTTP publisher (behind the PublisherFn seam)
# ---------------------------------------------------------------------------


class SigV4EventPublisher:
    """Default :data:`PublisherFn` that SigV4-signs an AppSync Events publish.

    Signs a POST to ``{endpoint}/event`` for the ``appsync`` service and sends
    it with the standard library HTTP client, so no third-party HTTP dependency
    is required. Constructed lazily by :func:`_default_publisher` only when a
    publish actually happens, so importing this module never requires AWS
    configuration (tests inject a fake publisher instead).

    Attributes:
        endpoint: The AppSync Events HTTP endpoint base URL.
        region: The signing region.
    """

    def __init__(
        self,
        endpoint: str,
        region: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        """Initialize the publisher.

        Args:
            endpoint: The AppSync Events HTTP endpoint base URL (from
                ``REALTIME_HTTP_ENDPOINT``).
            region: Signing region; falls back to ``AWS_REGION``.
            session: An optional boto3 ``Session`` supplying credentials;
                created lazily when omitted.
        """
        self.endpoint = endpoint.rstrip("/")
        self.region = region or get_optional_env(ENV_AWS_REGION, "us-east-1")
        self._session = session

    def _credentials(self) -> Any:
        """Return credentials from the boto3 session, creating it lazily.

        Returns:
            The resolved botocore credentials.
        """
        if self._session is None:
            import boto3

            self._session = boto3.Session()
        return self._session.get_credentials()

    def __call__(
        self, channel: str, events: Sequence[Mapping[str, Any]]
    ) -> None:
        """SigV4-sign and POST a single publish batch to AppSync Events.

        Args:
            channel: The channel path to publish to.
            events: The batch of events (already chunked to <= 5).

        Raises:
            Exception: Any signing or transport error; :func:`publish` catches
                it so the failure never reaches the originating operation.
        """
        import urllib.request

        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = f"{self.endpoint}/event"
        # AppSync Events expects each event as a JSON-encoded string.
        payload = json.dumps(
            {"channel": channel, "events": [json.dumps(event) for event in events]}
        )
        aws_request = AWSRequest(
            method="POST",
            url=url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(self._credentials(), "appsync", self.region).add_auth(aws_request)
        http_request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            url,
            data=payload.encode("utf-8"),
            headers=dict(aws_request.headers.items()),
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=5) as response:  # noqa: S310
            response.read()


def _default_publisher() -> Optional[PublisherFn]:
    """Return the default SigV4 publisher, or ``None`` when unconfigured.

    Returns:
        A :class:`SigV4EventPublisher` when ``REALTIME_HTTP_ENDPOINT`` is set,
        otherwise ``None`` (realtime publishing is skipped, best-effort).
    """
    endpoint = get_optional_env(ENV_REALTIME_HTTP_ENDPOINT)
    if not endpoint:
        logger.warning(
            "REALTIME_HTTP_ENDPOINT is not set; skipping realtime publish"
        )
        return None
    return SigV4EventPublisher(endpoint)


def publish(
    channel: str,
    events: Sequence[Mapping[str, Any]],
    *,
    publisher: Optional[PublisherFn] = None,
) -> bool:
    """Publish events to a channel, best-effort, batched to <= 5 per call.

    Chunks the events into batches of :data:`MAX_EVENTS_PER_PUBLISH`, then calls
    the publisher for each batch. A publisher failure is logged with the channel
    and never raised, so the originating operation is unaffected (design
    Component 4a error handling).

    Args:
        channel: The channel path to publish to.
        events: The events to publish (any length; chunked internally).
        publisher: The publisher seam; the default SigV4 publisher is used when
            omitted. When no publisher can be resolved, publishing is skipped.

    Returns:
        ``True`` if every batch published successfully, ``False`` if any batch
        failed or publishing was skipped (no configured publisher).
    """
    if not events:
        return True
    active = publisher if publisher is not None else _default_publisher()
    if active is None:
        return False
    all_ok = True
    for batch in chunk_events(events):
        try:
            active(channel, batch)
        except Exception as exc:  # noqa: BLE001 - best-effort, never block caller
            all_ok = False
            logger.error(
                "Realtime publish failed; open clients will reconcile on reconnect",
                extra={
                    "channel": channel,
                    "batchSize": len(batch),
                    "alertIds": [event.get("alertId") for event in batch],
                    "error": str(exc),
                },
            )
    return all_ok


def realtime_publish(
    event_type: str,
    item: Mapping[str, Any],
    *,
    unicast_gm_alias: Optional[str] = None,
    publisher: Optional[PublisherFn] = None,
) -> None:
    """Publish an alert event to its property channel (and optional unicast).

    This is the convenience helper the Escalation Service and Action Executor
    import: it builds the full-enough event from a ``pulse-alerts`` item and
    publishes it to the property broadcast channel. When ``unicast_gm_alias`` is
    supplied (an escalation-targeted nudge), the same event is also published to
    that recipient's per-user channel so only their device is nudged.

    Args:
        event_type: The event type (``ALERT_CREATED`` / ``ALERT_UPDATED`` /
            ``ALERT_RESOLVED``).
        item: The ``pulse-alerts`` item to build the event from.
        unicast_gm_alias: When set, also publish to the recipient's per-user
            channel (used for escalation nudges).
        publisher: The publisher seam; the default SigV4 publisher is used when
            omitted.
    """
    event = event_from_item(item, event_type)
    property_id = item["propertyId"]
    publish(broadcast_channel(property_id), [event], publisher=publisher)
    if unicast_gm_alias:
        publish(
            unicast_channel(property_id, unicast_gm_alias),
            [event],
            publisher=publisher,
        )


__all__ = [
    "ENV_REALTIME_HTTP_ENDPOINT",
    "NAMESPACE",
    "MAX_EVENTS_PER_PUBLISH",
    "EVENT_ALERT_CREATED",
    "EVENT_ALERT_UPDATED",
    "EVENT_ALERT_RESOLVED",
    "PublisherFn",
    "broadcast_channel",
    "unicast_channel",
    "build_event",
    "event_from_item",
    "chunk_events",
    "SigV4EventPublisher",
    "publish",
    "realtime_publish",
]
