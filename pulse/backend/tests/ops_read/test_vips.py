"""Unit tests for the VIPs tab shaping (``GET /vips``).

Verifies that ``shape_vips`` groups VIP arrivals by loyalty tier ordered by
eliteness, preserves each guest's profile fields and preferences, strips
``sensitiveNotes``, and scopes the tool call by ``propertyId``.
"""

from __future__ import annotations

from pulse.ops_read.vips import shape_vips
from tests.ops_read.conftest import RecordingToolCaller, ok

_PID = "ALOHA-CHI-001"


def _vip_guests_result() -> dict[str, object]:
    """Build a canned get_vip_guests result with mixed tiers and preferences."""
    return ok(
        {
            "date": "2026-08-18",
            "vipCount": 3,
            "guests": [
                {
                    "guestId": "G-1",
                    "guestName": "Ada Byron",
                    "loyaltyTier": "PLATINUM",
                    "roomNumber": "410",
                    "roomType": "KING",
                    "preferences": ["HIGH_FLOOR", "FEATHER_FREE"],
                    "specialOccasion": None,
                    "sensitiveNotes": "should never leak",
                },
                {
                    "guestId": "G-2",
                    "guestName": "Grace Hopper",
                    "loyaltyTier": "AMBASSADOR",
                    "roomNumber": "1201",
                    "roomType": "SUITE",
                    "preferences": ["CHAMPAGNE_ARRIVAL"],
                    "specialOccasion": "ANNIVERSARY",
                },
                {
                    "guestId": "G-3",
                    "guestName": "Alan Turing",
                    "loyaltyTier": "TITANIUM",
                    "roomNumber": "905",
                    "roomType": "KING_DELUXE",
                    "preferences": [],
                },
            ],
        }
    )


def test_shape_vips_groups_by_tier_in_eliteness_order() -> None:
    """Guests are grouped by tier, ordered AMBASSADOR > TITANIUM > PLATINUM."""
    caller = RecordingToolCaller({"get_vip_guests": _vip_guests_result()})

    result = shape_vips(_PID, caller)

    assert result["propertyId"] == _PID
    assert result["date"] == "2026-08-18"
    assert result["vipCount"] == 3
    tiers = [group["tier"] for group in result["tiers"]]
    assert tiers == ["AMBASSADOR", "TITANIUM", "PLATINUM"]
    counts = {group["tier"]: group["count"] for group in result["tiers"]}
    assert counts == {"AMBASSADOR": 1, "TITANIUM": 1, "PLATINUM": 1}


def test_shape_vips_preserves_preferences_and_strips_sensitive_notes() -> None:
    """Preferences/profile fields are preserved; sensitiveNotes is removed."""
    caller = RecordingToolCaller({"get_vip_guests": _vip_guests_result()})

    result = shape_vips(_PID, caller)

    platinum_guest = next(
        group["guests"][0]
        for group in result["tiers"]
        if group["tier"] == "PLATINUM"
    )
    assert platinum_guest["preferences"] == ["HIGH_FLOOR", "FEATHER_FREE"]
    assert platinum_guest["guestName"] == "Ada Byron"
    assert platinum_guest["roomNumber"] == "410"
    # Defense-in-depth: the sensitive field is never returned to the client.
    assert "sensitiveNotes" not in platinum_guest


def test_shape_vips_scopes_tool_call_by_property_id() -> None:
    """The get_vip_guests call is scoped with the correct propertyId."""
    caller = RecordingToolCaller({"get_vip_guests": _vip_guests_result()})

    shape_vips(_PID, caller)

    assert caller.calls == [("get_vip_guests", {"propertyId": _PID})]
    assert caller.property_ids() == [_PID]


def test_shape_vips_handles_empty_arrivals() -> None:
    """An empty VIP list yields zero tiers and a zero count."""
    caller = RecordingToolCaller(
        {"get_vip_guests": ok({"date": "2026-08-18", "vipCount": 0, "guests": []})}
    )

    result = shape_vips(_PID, caller)

    assert result["vipCount"] == 0
    assert result["tiers"] == []
