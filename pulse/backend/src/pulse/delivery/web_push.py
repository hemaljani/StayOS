"""Web Push (VAPID) background wake-up channel (Component 4b).

This module delivers a notification to a GM device whose PWA is closed or
backgrounded -- the "a new CRITICAL alert fired" wake-up within the Requirement
13 latency targets. It is intentionally the *background* channel; the foreground
live feed is served by :mod:`pulse.delivery.realtime_publish` (AppSync Events).

Design points enforced here:
    * **VAPID key from Secrets Manager.** The VAPID private key is loaded from
      the secret named by ``VAPID_SECRET_NAME`` (default ``pulse/webpush/vapid``)
      and is **never logged** (PYQUALITY-03, Security section).
    * **Payload shape (Requirement 13.5).** The push payload carries the
      ``alertId``, ``tier``, the ``title`` truncated to at most 100 characters,
      and the ``detail`` truncated to at most 500 characters (Property 22).
    * **Tier gating.** CRITICAL and WARNING alerts are pushed immediately; INFO
      alerts are not pushed here -- they are accumulated and flushed by
      :mod:`pulse.delivery.info_batcher` (Requirement 13.3).
    * **Retry + exhaustion (Requirements 13.6, 13.7).** Delivery to each
      subscription is retried up to 3 times at 10-second intervals. Every
      failure is logged with the ``alertId``; on exhaustion a delivery-exhausted
      event is recorded and the alert is retained for retrieval when the GM next
      opens the app.

The actual ``pywebpush`` call and the Secrets Manager / DynamoDB reads are kept
behind injectable seams so the pure payload construction and the retry policy
are unit-testable without AWS or a live push endpoint.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from pulse.common.config import get_optional_env
from pulse.common.logging import get_logger
from pulse.common.models import AlertTier

logger = get_logger("pulse-push-service")

# Secret holding the VAPID private key (never hardcoded; never logged).
ENV_VAPID_SECRET_NAME = "VAPID_SECRET_NAME"
DEFAULT_VAPID_SECRET_NAME = "pulse/webpush/vapid"

# The mailto: subject claim required by VAPID; configurable per deployment.
ENV_VAPID_SUBJECT = "VAPID_SUBJECT"
DEFAULT_VAPID_SUBJECT = "mailto:ops@stayos.example.com"

# Requirement 13.5 length bounds for the push payload.
TITLE_MAX_LEN = 100
DETAIL_MAX_LEN = 500

# Requirement 13.6 retry policy: up to 3 attempts at 10-second intervals.
WEB_PUSH_MAX_ATTEMPTS = 3
WEB_PUSH_RETRY_INTERVAL_SEC = 10

# Tiers eligible for an immediate background Web Push (Requirement 13.1/13.2).
# INFO is delivered via the batcher, not here.
_PUSH_ELIGIBLE_TIERS = frozenset({AlertTier.CRITICAL, AlertTier.WARNING})

# A push sender takes a subscription info dict and the JSON payload string and
# performs the delivery, raising on failure. Injectable so tests never call a
# live push endpoint. The default sender wraps ``pywebpush.webpush``.
PushSenderFn = Callable[[dict[str, Any], str], None]


@dataclass
class WebPushResult:
    """Outcome of delivering one alert to a set of subscriptions.

    Attributes:
        alert_id: The alert that was delivered.
        delivered_endpoints: Endpoints that accepted the push.
        exhausted_endpoints: Endpoints whose delivery exhausted all retries.
        skipped: ``True`` when the alert tier is not push-eligible (INFO), so no
            delivery was attempted.
    """

    alert_id: str
    delivered_endpoints: list[str] = field(default_factory=list)
    exhausted_endpoints: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def exhausted(self) -> bool:
        """Return whether any subscription exhausted its retries.

        Returns:
            ``True`` if at least one endpoint could not be delivered to.
        """
        return bool(self.exhausted_endpoints)


def should_web_push(tier: AlertTier) -> bool:
    """Return whether an alert of this tier gets an immediate Web Push.

    Args:
        tier: The alert tier.

    Returns:
        ``True`` for CRITICAL and WARNING; ``False`` for INFO (batched).
    """
    return tier in _PUSH_ELIGIBLE_TIERS


def build_push_payload(
    *, alert_id: str, tier: str, title: str, detail: str
) -> dict[str, Any]:
    """Build the Web Push payload with the Requirement 13.5 fields and bounds.

    Args:
        alert_id: The alert identifier.
        tier: The alert tier string.
        title: The alert title; truncated to 100 characters.
        detail: The alert detail; truncated to 500 characters.

    Returns:
        A JSON-serializable payload dict (camelCase keys, NAMING-05).
    """
    return {
        "alertId": alert_id,
        "tier": tier,
        "title": title[:TITLE_MAX_LEN],
        "detail": detail[:DETAIL_MAX_LEN],
    }


def payload_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Build a Web Push payload from a ``pulse-alerts`` item.

    Args:
        item: A ``pulse-alerts`` DynamoDB item (camelCase attributes).

    Returns:
        The truncated, JSON-serializable push payload.
    """
    return build_push_payload(
        alert_id=item["alertId"],
        tier=str(item.get("tier", "")),
        title=str(item.get("title", "")),
        detail=str(item.get("detail", "")),
    )


def _subscription_info(subscription: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``pywebpush`` subscription-info shape from a stored item.

    Args:
        subscription: A ``pulse-push-subscriptions`` item carrying ``endpoint``
            and the ``p256dh`` / ``auth`` keys.

    Returns:
        A ``{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}`` dict.
    """
    return {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription.get("p256dh"),
            "auth": subscription.get("auth"),
        },
    }


def _deliver_one(
    subscription: dict[str, Any],
    payload_json: str,
    alert_id: str,
    sender: PushSenderFn,
    *,
    max_attempts: int,
    interval_sec: int,
    sleep: Callable[[float], None],
) -> bool:
    """Deliver one payload to one subscription with bounded retries.

    Args:
        subscription: The subscription item.
        payload_json: The JSON-encoded push payload.
        alert_id: The alert being delivered (for log context).
        sender: The push-sender seam.
        max_attempts: Maximum delivery attempts.
        interval_sec: Seconds to wait between attempts.
        sleep: Sleep function, injectable for tests.

    Returns:
        ``True`` on success, ``False`` when all attempts were exhausted.
    """
    info = _subscription_info(subscription)
    endpoint = subscription.get("endpoint", "")
    for attempt in range(1, max_attempts + 1):
        try:
            sender(info, payload_json)
            return True
        except Exception as exc:  # noqa: BLE001 - retry policy needs any failure
            logger.error(
                "Web Push delivery attempt failed",
                extra={
                    "alertId": alert_id,
                    "endpoint": endpoint,
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "error": str(exc),
                },
            )
            if attempt < max_attempts:
                sleep(interval_sec)
    return False


def deliver_web_push(
    payload: dict[str, Any],
    subscriptions: Sequence[dict[str, Any]],
    *,
    sender: PushSenderFn,
    max_attempts: int = WEB_PUSH_MAX_ATTEMPTS,
    interval_sec: int = WEB_PUSH_RETRY_INTERVAL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> WebPushResult:
    """Deliver a payload to every subscription, with per-subscription retries.

    Each subscription is retried up to ``max_attempts`` times at ``interval_sec``
    intervals; failures are logged with the ``alertId``. Endpoints that exhaust
    their retries are recorded so the caller can mark the alert delivery-
    exhausted and retain it for in-app retrieval (Requirements 13.6, 13.7).

    Args:
        payload: The push payload (already truncated per Requirement 13.5).
        subscriptions: The GM's stored Web Push subscription items.
        sender: The push-sender seam (wraps ``pywebpush``); injectable.
        max_attempts: Maximum attempts per subscription (default 3).
        interval_sec: Seconds between attempts (default 10).
        sleep: Sleep function, injectable for tests.

    Returns:
        A :class:`WebPushResult` summarizing delivered and exhausted endpoints.
    """
    alert_id = str(payload.get("alertId", ""))
    payload_json = json.dumps(payload)
    result = WebPushResult(alert_id=alert_id)
    for subscription in subscriptions:
        endpoint = subscription.get("endpoint", "")
        delivered = _deliver_one(
            subscription,
            payload_json,
            alert_id,
            sender,
            max_attempts=max_attempts,
            interval_sec=interval_sec,
            sleep=sleep,
        )
        if delivered:
            result.delivered_endpoints.append(endpoint)
        else:
            result.exhausted_endpoints.append(endpoint)
            # Requirement 13.7: record delivery-exhausted; the alert is retained
            # in pulse-alerts for retrieval when the GM next opens the app.
            logger.error(
                "Web Push delivery exhausted; alert retained for in-app retrieval",
                extra={"alertId": alert_id, "endpoint": endpoint},
            )
    return result


# ---------------------------------------------------------------------------
# Default seams (Secrets Manager VAPID key + pywebpush sender)
# ---------------------------------------------------------------------------


def _vapid_secret_name() -> str:
    """Return the configured VAPID secret name.

    Returns:
        The value of ``VAPID_SECRET_NAME``, or the documented default.
    """
    return get_optional_env(ENV_VAPID_SECRET_NAME, DEFAULT_VAPID_SECRET_NAME)


def load_vapid_private_key(secret_name: Optional[str] = None) -> str:
    """Load the VAPID private key from Secrets Manager (never logged).

    The secret value may be either the raw private key string or a JSON object
    with a ``privateKey`` field; both are supported. The key itself is never
    written to logs.

    Args:
        secret_name: The secret name; falls back to ``VAPID_SECRET_NAME``.

    Returns:
        The VAPID private key string.
    """
    from pulse.common.aws import get_client

    name = secret_name or _vapid_secret_name()
    client = get_client("secretsmanager")
    # Retrieve the VAPID private key used to sign Web Push requests. The value
    # is sensitive: it is passed straight to the signer and never logged.
    response = client.get_secret_value(SecretId=name)
    secret_string = response.get("SecretString", "")
    try:
        parsed = json.loads(secret_string)
    except (json.JSONDecodeError, TypeError):
        return secret_string
    if isinstance(parsed, dict):
        return str(parsed.get("privateKey", secret_string))
    return secret_string


def make_default_sender(
    private_key: str, subject: Optional[str] = None
) -> PushSenderFn:
    """Build the default ``pywebpush``-backed push sender.

    Args:
        private_key: The VAPID private key (from :func:`load_vapid_private_key`).
        subject: The VAPID ``sub`` claim (``mailto:``); falls back to
            ``VAPID_SUBJECT``.

    Returns:
        A :data:`PushSenderFn` that delivers a payload to one subscription.
    """
    vapid_subject = subject or get_optional_env(
        ENV_VAPID_SUBJECT, DEFAULT_VAPID_SUBJECT
    )

    def _send(subscription_info: dict[str, Any], payload_json: str) -> None:
        """Deliver a single Web Push via ``pywebpush``.

        Args:
            subscription_info: The ``pywebpush`` subscription-info dict.
            payload_json: The JSON-encoded payload.
        """
        from pywebpush import webpush

        webpush(
            subscription_info=subscription_info,
            data=payload_json,
            vapid_private_key=private_key,
            vapid_claims={"sub": vapid_subject},
        )

    return _send


__all__ = [
    "ENV_VAPID_SECRET_NAME",
    "DEFAULT_VAPID_SECRET_NAME",
    "ENV_VAPID_SUBJECT",
    "TITLE_MAX_LEN",
    "DETAIL_MAX_LEN",
    "WEB_PUSH_MAX_ATTEMPTS",
    "WEB_PUSH_RETRY_INTERVAL_SEC",
    "PushSenderFn",
    "WebPushResult",
    "should_web_push",
    "build_push_payload",
    "payload_from_item",
    "deliver_web_push",
    "load_vapid_private_key",
    "make_default_sender",
]
