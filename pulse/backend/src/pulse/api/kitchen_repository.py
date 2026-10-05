"""Property-scoped kitchen snapshot reads for the PULSE REST API (Req 16.6).

Backs ``GET /kitchen?propertyId=`` for the Kitchen tab. The kitchen snapshot
(banquet countdown, F&B stats, delivery SLA, in-flight orders, and revenue
channel mix) is stored as a single ``pulse-kitchen`` item per property, keyed by
``propertyId`` only. Every read is scoped **server-side** to the caller's
associated properties so a client can never see the kitchen snapshot for a
property it is not entitled to (Property 25 / Requirement 16.6), mirroring the
defensive pattern of :func:`pulse.api.alerts_repository.get_alert`.

The banquet countdown is refreshed to a **live** value at read time (see
:func:`live_banquet_countdown`): the stored ``minutesRemaining`` /
``progressPct`` are only a static fallback, so a snapshot seeded days ago never
shows a frozen (or negative) countdown in the demo.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Optional

from pulse.api.identity import CallerIdentity
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger

logger = get_logger("pulse-api")

# A table getter seam so tests can inject a fake table without a live resource.
TableGetterFn = Callable[[str], Any]

# The banquet "service opens" cadence used to keep the countdown live: service
# opens on the hour, every this-many minutes (a rolling demo window), so the
# countdown always shows a plausible time-to-dispatch regardless of how long ago
# the snapshot was seeded.
_BANQUET_WINDOW_MINUTES = 45


def live_banquet_countdown(
    stored: Optional[dict[str, Any]], now: datetime
) -> Optional[dict[str, Any]]:
    """Return the banquet countdown with a live ``minutesRemaining`` / progress.

    Recomputes the countdown against a rolling window so the Kitchen tab never
    shows a frozen or negative timer: service is treated as opening every
    ``_BANQUET_WINDOW_MINUTES`` minutes, and the remaining time counts down to
    the next opening. ``progressPct`` tracks how far into the current window we
    are (fuller as dispatch nears). All other stored fields (title, badge,
    subline) are preserved; the subline's dispatch minutes are refreshed too.

    Args:
        stored: The stored ``banquetCountdown`` sub-payload, or ``None``.
        now: The current time (injected for deterministic tests).

    Returns:
        A new countdown dict with live values, or ``None`` when ``stored`` is
        ``None`` (no banquet countdown on this snapshot).
    """
    if not stored:
        return stored
    minutes_into_window = (now.minute % _BANQUET_WINDOW_MINUTES) + now.second / 60.0
    minutes_remaining = max(1, round(_BANQUET_WINDOW_MINUTES - minutes_into_window))
    progress_pct = max(
        1, min(99, round(minutes_into_window / _BANQUET_WINDOW_MINUTES * 100))
    )
    live = dict(stored)
    live["minutesRemaining"] = minutes_remaining
    live["badge"] = "On Track" if minutes_remaining >= 12 else "Tight"
    live["progressPct"] = progress_pct
    title = stored.get("title", "Banquet service")
    event = title.split(" \u00b7 ")[0] if " \u00b7 " in title else title
    live["subline"] = (
        f"{event} \u00b7 Kitchen dispatch in {minutes_remaining} min"
    )
    return live


def get_kitchen(
    identity: CallerIdentity,
    property_id: str,
    *,
    kitchen_table_name: str,
    table_getter: TableGetterFn = get_table,
) -> Optional[dict[str, Any]]:
    """Fetch a property's kitchen snapshot, scoped to the caller's properties.

    Returns the full ``pulse-kitchen`` item (the nested banquet countdown, F&B
    stats, delivery SLA, in-flight orders, and channel mix) only when the caller
    is associated with the requested property; otherwise returns ``None`` so an
    out-of-scope or missing snapshot is indistinguishable to the client and no
    cross-property existence is leaked (Property 25, Requirement 16.6).

    Args:
        identity: The authenticated caller.
        property_id: The property whose kitchen snapshot to read.
        kitchen_table_name: The ``pulse-kitchen`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        The kitchen snapshot item, or ``None`` when not found or out of scope.
    """
    # Defense in depth: refuse a read for a property the caller does not own,
    # even if a caller reached here without the handler's scope check.
    if not identity.is_associated_with(property_id):
        logger.warning(
            "Kitchen access denied by property scope",
            extra={"gmAlias": identity.gm_alias, "propertyId": property_id},
        )
        return None
    table = table_getter(kitchen_table_name)
    # Single-item read: one snapshot per property, keyed by propertyId only.
    item = table.get_item(Key={"propertyId": property_id}).get("Item")
    if item is None:
        return None
    # Re-verify the returned item's property is in the caller's set (Property 25).
    if item.get("propertyId") not in identity.properties:
        return None
    # Overlay a live banquet countdown so a snapshot seeded long ago never shows
    # a frozen or negative timer (the stored value is only a fallback).
    item["banquetCountdown"] = live_banquet_countdown(
        item.get("banquetCountdown"), datetime.now(UTC)
    )
    return item


__all__ = [
    "TableGetterFn",
    "get_kitchen",
    "live_banquet_countdown",
]
