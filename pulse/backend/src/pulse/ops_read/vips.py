"""Shape ``get_vip_guests`` results for the PWA VIPs tab.

The VIPs tab (Component 6) renders VIP arrivals grouped by loyalty tier, with
each guest's profile fields and preferences shown in a profile modal. This
module turns the shared Gateway ``get_vip_guests`` tool result into that grouped
shape, ordered by tier eliteness, preserving every guest profile field (name,
initials, loyalty number, total stays, room, special occasion, preferences,
account type) while defensively stripping ``sensitiveNotes`` so PII is never
returned to the client (PYQUALITY-03).

The single dependency is the :data:`~pulse.ops_read.gateway.ToolCaller` seam, so
this shaping is fully unit-testable with an in-memory fake and never opens a
network connection.
"""

from __future__ import annotations

from typing import Any

from pulse.common.logging import get_logger
from pulse.ops_read.gateway import ToolCaller, tool_data

logger = get_logger("pulse-ops-read")

# The Gateway tool the VIPs tab reads from.
VIP_TOOL_NAME = "get_vip_guests"

# LUMI loyalty tiers in descending eliteness. Tiers present in the data are
# emitted in this order first; any unknown tier is appended alphabetically so
# the grouping is deterministic regardless of the tool's ordering.
TIER_ORDER = ("AMBASSADOR", "TITANIUM", "PLATINUM")

# Guest field that must never be returned to the client (the shared tool already
# strips it; this is a defense-in-depth removal).
_SENSITIVE_FIELD = "sensitiveNotes"


def _clean_guest(guest: dict[str, Any]) -> dict[str, Any]:
    """Return a guest dict with the sensitive-notes field removed.

    Args:
        guest: A VIP guest entry from ``get_vip_guests``.

    Returns:
        A shallow copy of the guest without ``sensitiveNotes``.
    """
    return {key: value for key, value in guest.items() if key != _SENSITIVE_FIELD}


def _tier_key(tier: str) -> tuple[int, str]:
    """Build a sort key ordering known tiers by eliteness, others after.

    Args:
        tier: The upper-cased loyalty tier name.

    Returns:
        A ``(rank, tier)`` tuple: known tiers get their index in
        :data:`TIER_ORDER`; unknown tiers sort after all known ones, then
        alphabetically.
    """
    if tier in TIER_ORDER:
        return (TIER_ORDER.index(tier), tier)
    return (len(TIER_ORDER), tier)


def shape_vips(property_id: str, tool_caller: ToolCaller) -> dict[str, Any]:
    """Assemble the ``GET /vips`` response from the VIP-guests Gateway tool.

    Calls ``get_vip_guests`` scoped to ``property_id`` and groups the returned
    guests by loyalty tier, ordered by eliteness. Each guest's profile fields
    and preferences are preserved (``sensitiveNotes`` stripped).

    Args:
        property_id: The property to scope the tool call to (server-side scope).
        tool_caller: The Gateway tool-call seam.

    Returns:
        The VIPs response body::

            {
              "propertyId": str,
              "date": str | None,
              "vipCount": int,
              "tiers": [
                {"tier": str, "count": int, "guests": [ {profile fields...} ]}
              ]
            }

    Raises:
        OpsReadFailure: If the Gateway tool is unavailable (propagated from
            :func:`~pulse.ops_read.gateway.tool_data`).
    """
    raw = tool_caller(VIP_TOOL_NAME, {"propertyId": property_id})
    data = tool_data(raw, VIP_TOOL_NAME)

    guests = data.get("guests") or []
    groups: dict[str, list[dict[str, Any]]] = {}
    for guest in guests:
        if not isinstance(guest, dict):
            continue
        tier = str(guest.get("loyaltyTier") or "UNKNOWN").upper()
        groups.setdefault(tier, []).append(_clean_guest(guest))

    tiers = [
        {"tier": tier, "count": len(groups[tier]), "guests": groups[tier]}
        for tier in sorted(groups, key=_tier_key)
    ]

    logger.info(
        "Shaped VIPs response",
        extra={
            "property_id": property_id,
            "vipCount": len(guests),
            "tierCount": len(tiers),
        },
    )
    return {
        "propertyId": property_id,
        "date": data.get("date"),
        "vipCount": data.get("vipCount", len(guests)),
        "tiers": tiers,
    }


__all__ = ["VIP_TOOL_NAME", "TIER_ORDER", "shape_vips"]
