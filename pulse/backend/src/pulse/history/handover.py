"""Shift-handover window query over ``pulse-alert-history`` (Requirement 14.3).

The shift-handover log lists every alert for a property whose creation time
falls within a shift window, ordered by creation time descending (newest first).
The pure :func:`select_in_window` carries the Requirement 14.3 contract (exactly
the in-window alerts, ordered by ``createdAt`` descending -- Property 24), and
:func:`query_shift_handover` backs it with a ``propertyId-createdAt-index`` query
(``Key.between`` + ``ScanIndexForward=False``) for the ``GET /shift-handover``
route.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger

logger = get_logger("pulse-history-writer")

# The index that orders a property's history by creation time.
PROPERTY_CREATED_INDEX = "propertyId-createdAt-index"


def select_in_window(
    alerts: Sequence[dict[str, Any]], start_iso: str, end_iso: str
) -> list[dict[str, Any]]:
    """Return the in-window alerts ordered by ``createdAt`` descending (pure).

    The window is inclusive of both bounds. This is the reference behavior the
    ``propertyId-createdAt-index`` query reproduces and the target of Property
    24.

    Args:
        alerts: Candidate alert/history items (each with a ``createdAt``).
        start_iso: The window start (ISO 8601), inclusive.
        end_iso: The window end (ISO 8601), inclusive.

    Returns:
        Exactly the items whose ``createdAt`` is within ``[start, end]``, sorted
        by ``createdAt`` descending.
    """
    in_window = [
        alert
        for alert in alerts
        if start_iso <= str(alert.get("createdAt", "")) <= end_iso
    ]
    return sorted(
        in_window, key=lambda alert: str(alert.get("createdAt", "")), reverse=True
    )


def query_shift_handover(
    property_id: str,
    start_iso: str,
    end_iso: str,
    *,
    history_table_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Query the shift-handover log for a property within a window.

    Uses the ``propertyId-createdAt-index`` with a ``between`` key condition and
    descending order so the result is exactly the in-window alerts, newest
    first, without post-filtering (Requirement 14.3, Property 24).

    Args:
        property_id: The property to query.
        start_iso: The window start (ISO 8601), inclusive.
        end_iso: The window end (ISO 8601), inclusive.
        history_table_name: The ``pulse-alert-history`` table name; read from
            configuration when omitted.

    Returns:
        The in-window history items ordered by ``createdAt`` descending.
    """
    from boto3.dynamodb.conditions import Key

    from pulse.common.config import load_config

    name = history_table_name or load_config().alert_history_table
    table = get_table(name)
    response = table.query(
        IndexName=PROPERTY_CREATED_INDEX,
        KeyConditionExpression=(
            Key("propertyId").eq(property_id)
            & Key("createdAt").between(start_iso, end_iso)
        ),
        ScanIndexForward=False,
    )
    items = list(response.get("Items", []))
    logger.info(
        "Shift-handover query complete",
        extra={
            "propertyId": property_id,
            "from": start_iso,
            "to": end_iso,
            "count": len(items),
        },
    )
    return items


__all__ = [
    "PROPERTY_CREATED_INDEX",
    "select_in_window",
    "query_shift_handover",
]
