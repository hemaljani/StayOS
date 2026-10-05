"""Web Push subscription registration: ``POST /push-subscriptions``.

Registers a browser's Web Push (VAPID) subscription so the delivery layer can
wake a closed/backgrounded PWA for CRITICAL/WARNING alerts (design Component
4b). Subscriptions are stored in ``pulse-push-subscriptions`` keyed by
``gmAlias`` (partition) + ``endpointHash`` (sort), with the caller's associated
properties recorded for scoping. The subscription is always registered under the
authenticated caller's identity, never a client-supplied alias, so a client
cannot register a subscription on another user's behalf (Requirement 16.6).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pulse.api.identity import CallerIdentity
from pulse.common.dynamo import get_table
from pulse.common.errors import PulseError
from pulse.common.logging import get_logger

logger = get_logger("pulse-api")

# Fields a valid Web Push subscription must carry (endpoint + encryption keys).
_REQUIRED_SUBSCRIPTION_FIELDS = ("endpoint", "p256dh", "auth")


class InvalidSubscriptionError(PulseError):
    """Raised when a submitted Web Push subscription is missing required fields.

    Attributes:
        missing: The name of the first missing required field.
    """

    def __init__(self, message: str, missing: str) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description.
            missing: The first missing required field name.
        """
        super().__init__(message)
        self.missing = missing


def _endpoint_hash(endpoint: str) -> str:
    """Return a stable sort-key hash for a subscription endpoint.

    Args:
        endpoint: The push service endpoint URL.

    Returns:
        The hex SHA-256 digest of the endpoint (a stable, bounded sort key).
    """
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def build_subscription_item(
    body: Mapping[str, Any], identity: CallerIdentity, *, now: str
) -> dict[str, Any]:
    """Build the ``pulse-push-subscriptions`` item from a request body (pure).

    Args:
        body: The submitted subscription (``endpoint``, ``p256dh``, ``auth``).
        identity: The authenticated caller (owns the subscription).
        now: ISO 8601 UTC creation timestamp.

    Returns:
        The DynamoDB item (camelCase keys, NAMING-05).

    Raises:
        InvalidSubscriptionError: If a required subscription field is missing.
    """
    for required_field in _REQUIRED_SUBSCRIPTION_FIELDS:
        if not body.get(required_field):
            raise InvalidSubscriptionError(
                f"Subscription is missing required field {required_field!r}",
                missing=required_field,
            )
    endpoint = str(body["endpoint"])
    return {
        "gmAlias": identity.gm_alias,
        "endpointHash": _endpoint_hash(endpoint),
        "endpoint": endpoint,
        "p256dh": str(body["p256dh"]),
        "auth": str(body["auth"]),
        # Record the caller's associated properties for delivery-time scoping.
        "propertyIds": sorted(identity.properties),
        "createdAt": now,
    }


def register_subscription(
    body: Mapping[str, Any],
    identity: CallerIdentity,
    *,
    subscriptions_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
) -> dict[str, Any]:
    """Register a Web Push subscription for the authenticated caller.

    Args:
        body: The submitted subscription body.
        identity: The authenticated caller.
        subscriptions_table_name: The ``pulse-push-subscriptions`` table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        A result dict with ``registered`` and the ``endpointHash``.

    Raises:
        InvalidSubscriptionError: If the subscription is missing required fields.
    """
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    item = build_subscription_item(body, identity, now=now)
    table = table_getter(subscriptions_table_name)
    table.put_item(Item=item)
    logger.info(
        "Web Push subscription registered",
        extra={"gmAlias": identity.gm_alias, "endpointHash": item["endpointHash"]},
    )
    return {"registered": True, "endpointHash": item["endpointHash"]}


__all__ = [
    "InvalidSubscriptionError",
    "build_subscription_item",
    "register_subscription",
]
