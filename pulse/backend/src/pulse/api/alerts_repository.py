"""Property-scoped alert reads for the PULSE REST API (Requirement 16.6).

Backs ``GET /alerts`` (feed + tier/status filters) and ``GET /alerts/{alertId}``
(detail including the triage brief). Every read is scoped **server-side** to the
caller's associated properties so a client can never see an alert for a property
it is not entitled to (Property 25). The scoping decision is expressed as two
pure functions -- :func:`select_target_properties` and :func:`scope_alerts` --
so the entitlement contract is unit-testable without DynamoDB.

Query strategy (design Data Models GSIs):
    * The per-property feed uses ``propertyId-createdAt-index`` with
      ``ScanIndexForward=False`` so alerts come back newest-first (Requirement
      15.4) without a post-sort.
    * Tier and status filters (Requirement 15.5) are applied as filter
      expressions; tier cardinality is low, so no separate index is needed at
      prototype scale.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional

from pulse.api.identity import CallerIdentity
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger

logger = get_logger("pulse-api")

# The feed index: a property's alerts ordered by creation time.
PROPERTY_CREATED_INDEX = "propertyId-createdAt-index"

# A table getter seam so tests can inject a fake table without a live resource.
TableGetterFn = Callable[[str], Any]


def select_target_properties(
    requested_property: Optional[str], identity: CallerIdentity
) -> list[str]:
    """Resolve which properties a feed query should read (pure).

    When the caller requests a specific property, the query targets exactly that
    property **iff** the caller is associated with it (otherwise no property is
    targeted, yielding an empty, non-leaking result). When no property is
    requested, the query targets all of the caller's associated properties.

    Args:
        requested_property: The ``propertyId`` query-string value, if any.
        identity: The authenticated caller.

    Returns:
        The property ids to query (a subset of the caller's associated set).
    """
    if requested_property is not None:
        if identity.is_associated_with(requested_property):
            return [requested_property]
        # Requested a property the caller is not entitled to: read nothing.
        return []
    return sorted(identity.properties)


def scope_alerts(
    alerts: Iterable[dict[str, Any]], allowed_properties: frozenset[str]
) -> list[dict[str, Any]]:
    """Return only the alerts whose property is in the allowed set (pure).

    This is the last-line defensive filter guaranteeing Property 25: regardless
    of how the candidate alerts were gathered, none is returned for a property
    the caller is not associated with.

    Args:
        alerts: Candidate alert items (each with a ``propertyId``).
        allowed_properties: The caller's associated-property set.

    Returns:
        The subset of alerts whose ``propertyId`` is allowed.
    """
    return [
        alert
        for alert in alerts
        if alert.get("propertyId") in allowed_properties
    ]


def _matches_filters(
    alert: dict[str, Any], tier: Optional[str], status: Optional[str]
) -> bool:
    """Return whether an alert matches the optional tier/status filters.

    Args:
        alert: The alert item.
        tier: The tier filter (case-insensitive), or ``None`` for all tiers.
        status: The status filter (case-insensitive), or ``None`` for all.

    Returns:
        ``True`` when the alert satisfies both supplied filters.
    """
    if tier is not None and str(alert.get("tier", "")).upper() != tier.upper():
        return False
    if status is not None and str(alert.get("status", "")).upper() != status.upper():
        return False
    return True


def apply_filters(
    alerts: Iterable[dict[str, Any]],
    *,
    tier: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Filter alerts by optional tier and status (pure, Requirement 15.5).

    Args:
        alerts: The candidate alerts.
        tier: The tier filter, or ``None`` to keep all tiers.
        status: The status filter, or ``None`` to keep all statuses.

    Returns:
        The alerts matching both supplied filters.
    """
    return [alert for alert in alerts if _matches_filters(alert, tier, status)]


def _query_property_feed(
    table: Any, property_id: str, *, tier: Optional[str], status: Optional[str]
) -> list[dict[str, Any]]:
    """Query one property's feed newest-first with optional server filters.

    Args:
        table: The ``pulse-alerts`` table resource.
        property_id: The property whose feed to read.
        tier: Optional tier filter applied as a DynamoDB filter expression.
        status: Optional status filter applied as a DynamoDB filter expression.

    Returns:
        The property's alert items, newest-first.
    """
    from boto3.dynamodb.conditions import Attr, Key

    query_kwargs: dict[str, Any] = {
        "IndexName": PROPERTY_CREATED_INDEX,
        "KeyConditionExpression": Key("propertyId").eq(property_id),
        "ScanIndexForward": False,
    }
    filters = []
    if tier is not None:
        filters.append(Attr("tier").eq(tier.upper()))
    if status is not None:
        # ``status`` is a reserved word, but Attr() handles the alias for us.
        filters.append(Attr("status").eq(status.upper()))
    if filters:
        expression = filters[0]
        for extra in filters[1:]:
            expression = expression & extra
        query_kwargs["FilterExpression"] = expression
    response = table.query(**query_kwargs)
    return list(response.get("Items", []))


def list_alerts(
    identity: CallerIdentity,
    *,
    requested_property: Optional[str] = None,
    tier: Optional[str] = None,
    status: Optional[str] = None,
    alerts_table_name: str,
    table_getter: TableGetterFn = get_table,
) -> list[dict[str, Any]]:
    """List alerts for the caller, scoped to their associated properties.

    Reads the feed for each in-scope property newest-first, applies the tier and
    status filters, and defensively re-scopes the merged result so no
    out-of-scope alert can be returned (Property 25).

    Args:
        identity: The authenticated caller.
        requested_property: Optional ``propertyId`` query filter.
        tier: Optional tier filter (Requirement 15.5).
        status: Optional status filter.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        The scoped, filtered alert items (newest-first per property).
    """
    target_properties = select_target_properties(requested_property, identity)
    if not target_properties:
        return []
    table = table_getter(alerts_table_name)
    merged: list[dict[str, Any]] = []
    for property_id in target_properties:
        merged.extend(
            _query_property_feed(table, property_id, tier=tier, status=status)
        )
    scoped = scope_alerts(merged, identity.properties)
    logger.info(
        "Alert feed query complete",
        extra={
            "gmAlias": identity.gm_alias,
            "targetProperties": target_properties,
            "tier": tier,
            "status": status,
            "count": len(scoped),
        },
    )
    return scoped


def get_alert(
    identity: CallerIdentity,
    alert_id: str,
    *,
    alerts_table_name: str,
    table_getter: TableGetterFn = get_table,
) -> Optional[dict[str, Any]]:
    """Fetch a single alert by id, scoped to the caller's properties.

    Returns the full alert item (including the ``triageBrief``) only when the
    caller is associated with the alert's property; otherwise returns ``None``
    so an out-of-scope or missing alert is indistinguishable to the client and
    no cross-property existence is leaked (Property 25, Requirement 15.7).

    Args:
        identity: The authenticated caller.
        alert_id: The alert identifier.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        The alert item, or ``None`` when not found or out of scope.
    """
    table = table_getter(alerts_table_name)
    item = table.get_item(Key={"alertId": alert_id}).get("Item")
    if item is None:
        return None
    if item.get("propertyId") not in identity.properties:
        logger.warning(
            "Alert access denied by property scope",
            extra={"gmAlias": identity.gm_alias, "alertId": alert_id},
        )
        return None
    return item


__all__ = [
    "PROPERTY_CREATED_INDEX",
    "TableGetterFn",
    "select_target_properties",
    "scope_alerts",
    "apply_filters",
    "list_alerts",
    "get_alert",
]
