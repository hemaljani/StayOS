"""Delivery layer entry point (``pulse-push-service``): dual-channel delivery.

On each alert create or status change, this Lambda performs **two** publishes
for the alert (design Component 4, "one Lambda, two channels"):

    1. **Foreground realtime** -- an AppSync Events publish to the alert's
       property channel via :mod:`pulse.delivery.realtime_publish`, so every
       open PWA client updates its live feed instantly.
    2. **Background Web Push** -- for CRITICAL/WARNING alerts, a VAPID Web Push
       via :mod:`pulse.delivery.web_push`, so a closed/backgrounded device is
       woken within the Requirement 13 latency targets.

Following PYQUALITY-05, :func:`lambda_handler` is a thin dispatcher: it resolves
the alert item and the default seams (alert loader, subscription loader, Web
Push sender, realtime publisher), then delegates to the pure orchestration in
:func:`deliver_alert`. Every side effect is behind an injectable seam so the
orchestration is unit-testable without AWS or a live push endpoint.

This module also exposes :func:`make_escalation_deliver`, a factory that returns
a callable matching the Escalation Service's ``DeliverFn`` seam
(``Callable[[str, str], None]``) so escalation nudges reuse the same delivery
path (unicast realtime to the current recipient + a Web Push to that recipient).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from pulse.common.config import load_config
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import AlertTier
from pulse.common.tracing import get_tracer
from pulse.delivery import realtime_publish as rt
from pulse.delivery import web_push as wp
from pulse.observability import metrics as obs

logger = get_logger("pulse-push-service")
tracer = get_tracer("pulse-push-service")

# Seam type aliases.
AlertLoaderFn = Callable[[str], Optional[dict[str, Any]]]
SubscriptionLoaderFn = Callable[[str], list[dict[str, Any]]]
# Records the generation->delivery latency metric for a delivered alert. Kept
# behind a seam (default ``None``) so it fires only when the delivery Lambda
# wires it in -- callers/tests that omit it emit no metric.
LatencyRecorderFn = Callable[[dict[str, Any], str], None]


def _resolve_tier(item: dict[str, Any]) -> AlertTier:
    """Resolve the alert tier from an item, defaulting to INFO on absence.

    Args:
        item: The ``pulse-alerts`` item.

    Returns:
        The parsed :class:`AlertTier` (``INFO`` when the attribute is missing or
        unrecognized, so an unknown tier never triggers a background push).
    """
    try:
        return AlertTier(item.get("tier", AlertTier.INFO.value))
    except ValueError:
        return AlertTier.INFO


def deliver_alert(
    item: dict[str, Any],
    event_type: str,
    *,
    subscription_loader: SubscriptionLoaderFn,
    web_push_sender: wp.PushSenderFn,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    unicast_gm_alias: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
    latency_recorder: Optional[LatencyRecorderFn] = None,
) -> dict[str, Any]:
    """Deliver one alert over both channels (pure orchestration).

    Always publishes a realtime event to the property channel (and, when
    ``unicast_gm_alias`` is set, the recipient's per-user channel). For
    CRITICAL/WARNING alerts it additionally sends a Web Push to the owning GM's
    subscriptions with bounded retries. INFO alerts are not pushed here (the
    INFO batcher handles them), but their realtime event is still published.

    Args:
        item: The ``pulse-alerts`` item to deliver.
        event_type: The realtime event type (``ALERT_CREATED`` /
            ``ALERT_UPDATED`` / ``ALERT_RESOLVED``).
        subscription_loader: Loads a GM's Web Push subscriptions by alias.
        web_push_sender: The Web Push sender seam.
        realtime_publisher: The realtime publisher seam (default resolved when
            omitted).
        unicast_gm_alias: When set, also publish the realtime event to that
            recipient's per-user channel (escalation nudge).
        sleep: Sleep function passed to the Web Push retry (injectable).
        latency_recorder: Optional seam that records the generation->delivery
            latency metric after delivery (Requirement 17.1). When omitted no
            metric is emitted, so it never fires in callers/tests that do not
            wire it. It is invoked best-effort; the recorder itself must never
            raise into delivery (Requirement 17.4).

    Returns:
        A summary dict describing what was delivered on each channel.
    """
    # Channel 1: foreground realtime (best-effort, never raises).
    rt.realtime_publish(
        event_type,
        item,
        unicast_gm_alias=unicast_gm_alias,
        publisher=realtime_publisher,
    )

    tier = _resolve_tier(item)
    summary: dict[str, Any] = {
        "alertId": item.get("alertId"),
        "eventType": event_type,
        "realtimePublished": True,
        "webPushAttempted": False,
    }

    def _emit_latency() -> None:
        """Record the delivery-latency metric, best-effort (never raises).

        Requirement 17.4: a failure to emit the metric must not interrupt
        delivery. The wired recorder is itself best-effort; this second guard
        also protects delivery from any recorder passed in by a caller.
        """
        if latency_recorder is None:
            return
        try:
            latency_recorder(item, tier.value)
        except Exception as exc:  # noqa: BLE001 - metric emission must not block delivery
            logger.error(
                "Delivery latency recorder raised; delivery unaffected",
                extra={"alertId": item.get("alertId"), "error": str(exc)},
            )

    # Channel 2: background Web Push for CRITICAL/WARNING only.
    if not wp.should_web_push(tier):
        summary["webPushSkipped"] = True
        _emit_latency()
        return summary

    gm_alias = item.get("gmAlias")
    subscriptions = subscription_loader(gm_alias) if gm_alias else []
    payload = wp.payload_from_item(item)
    result = wp.deliver_web_push(
        payload, subscriptions, sender=web_push_sender, sleep=sleep
    )
    summary.update(
        {
            "webPushAttempted": True,
            "webPushDelivered": len(result.delivered_endpoints),
            "webPushExhausted": len(result.exhausted_endpoints),
        }
    )
    _emit_latency()
    return summary


# ---------------------------------------------------------------------------
# Default seams (DynamoDB loaders + VAPID-backed sender)
# ---------------------------------------------------------------------------


def _default_alert_loader(alerts_table_name: str) -> AlertLoaderFn:
    """Build the default alert loader over ``pulse-alerts``.

    Args:
        alerts_table_name: The ``pulse-alerts`` physical table name.

    Returns:
        A loader that returns the alert item for an id, or ``None``.
    """
    table = get_table(alerts_table_name)

    def _load(alert_id: str) -> Optional[dict[str, Any]]:
        return table.get_item(Key={"alertId": alert_id}).get("Item")

    return _load


def _default_subscription_loader(
    subscriptions_table_name: str,
) -> SubscriptionLoaderFn:
    """Build the default subscription loader over ``pulse-push-subscriptions``.

    Args:
        subscriptions_table_name: The ``pulse-push-subscriptions`` table name.

    Returns:
        A loader returning all Web Push subscription items for a ``gmAlias``.
    """
    from boto3.dynamodb.conditions import Key

    table = get_table(subscriptions_table_name)

    def _load(gm_alias: str) -> list[dict[str, Any]]:
        response = table.query(KeyConditionExpression=Key("gmAlias").eq(gm_alias))
        return list(response.get("Items", []))

    return _load


def _extract_item(
    event: dict[str, Any], alert_loader: AlertLoaderFn
) -> Optional[dict[str, Any]]:
    """Resolve the alert item from the invocation event.

    Accepts either an embedded ``alert`` item or an ``alertId`` to load.

    Args:
        event: The invocation event.
        alert_loader: The alert loader seam.

    Returns:
        The alert item, or ``None`` when it cannot be resolved.
    """
    embedded = event.get("alert")
    if isinstance(embedded, dict):
        return embedded
    alert_id = event.get("alertId")
    if alert_id:
        return alert_loader(alert_id)
    return None


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Delivery Lambda handler (thin dispatcher).

    Resolves the alert item (embedded or loaded by ``alertId``) and delivers it
    over both channels using the default DynamoDB loaders, the VAPID-backed Web
    Push sender, and the default realtime publisher.

    Args:
        event: The invocation event with an ``eventType`` and either an ``alert``
            item or an ``alertId``.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A delivery summary dict.
    """
    config = load_config()
    alert_loader = _default_alert_loader(config.alerts_table)
    item = _extract_item(event, alert_loader)
    if item is None:
        logger.warning(
            "No alert to deliver; event carried neither an item nor a known id",
            extra={"eventKeys": sorted(event.keys())},
        )
        return {"delivered": False}

    # Correlate this delivery segment with the alert so a single alert's trace
    # can be followed across the rule-eval -> triage -> delivery -> resolve hops
    # in the X-Ray service map (matches the LUMI annotation pattern).
    tracer.put_annotation(key="alertId", value=str(item.get("alertId", "")))

    event_type = event.get("eventType", rt.EVENT_ALERT_CREATED)
    subscription_loader = _default_subscription_loader(
        config.push_subscriptions_table
    )
    # Load the VAPID key once per cold start; it is never logged.
    private_key = wp.load_vapid_private_key()
    sender = wp.make_default_sender(private_key)

    summary = deliver_alert(
        item,
        event_type,
        subscription_loader=subscription_loader,
        web_push_sender=sender,
        unicast_gm_alias=event.get("unicastGmAlias"),
        latency_recorder=obs.record_delivery_latency,
    )
    logger.info("Alert delivery complete", extra=summary)
    return summary


def make_escalation_deliver(
    *,
    alert_loader: AlertLoaderFn,
    subscription_loader: SubscriptionLoaderFn,
    web_push_sender: wp.PushSenderFn,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[str, str], None]:
    """Build a delivery callable matching the Escalation Service ``DeliverFn``.

    The Escalation Service delivers an escalated alert to the *current* recipient
    via a ``Callable[[str, str], None]`` (``alertId``, ``recipientAlias``). This
    factory returns such a callable that reuses the dual-channel delivery path:
    it publishes an ``ALERT_UPDATED`` event to the property feed *and* to the
    recipient's per-user (unicast) escalation channel, and sends the recipient a
    Web Push. Raising on a hard failure lets the Escalation Service's own retry
    policy (Requirement 6.7) apply.

    Args:
        alert_loader: Loads the alert item by id.
        subscription_loader: Loads a recipient's Web Push subscriptions.
        web_push_sender: The Web Push sender seam.
        realtime_publisher: The realtime publisher seam.
        sleep: Sleep function passed to the Web Push retry.

    Returns:
        A ``DeliverFn``-compatible callable for the Escalation Service.
    """

    def _deliver(alert_id: str, recipient: str) -> None:
        item = alert_loader(alert_id)
        if item is None:
            raise ValueError(f"Cannot escalate unknown alert {alert_id!r}")
        deliver_alert(
            item,
            rt.EVENT_ALERT_UPDATED,
            subscription_loader=lambda _alias: subscription_loader(recipient),
            web_push_sender=web_push_sender,
            realtime_publisher=realtime_publisher,
            unicast_gm_alias=recipient,
            sleep=sleep,
        )

    return _deliver


__all__ = [
    "AlertLoaderFn",
    "SubscriptionLoaderFn",
    "LatencyRecorderFn",
    "deliver_alert",
    "lambda_handler",
    "make_escalation_deliver",
]
