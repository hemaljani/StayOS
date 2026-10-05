"""Curated Kitchen/F&B demo snapshot for the ``pulse-kitchen`` table.

This module holds the per-property Kitchen snapshot the Kitchen tab reads via
``GET /kitchen``. One snapshot item is stored per property (keyed by
``propertyId``) holding the whole Kitchen tab payload: the active banquet
countdown, today's F&B summary tiles, the delivery SLA tracker, the in-flight
order feed, and the revenue channel mix with its advisory note.

The values for ``ALOHA-CHI-001`` mirror what previously lived in the PWA at
``frontend/src/lib/kitchenDemoData.ts`` and match ``pulse-prototype.html``
(byte-identical to the retired standalone ``scripts/seed_kitchen.py``). Every
other pilot property gets its OWN distinct-but-stable dataset, deterministically
derived from the property id, so each demo GM sees a plausible, fresh-looking
kitchen instead of an obvious clone. "Deterministic" matters: a given property
always produces the same numbers, so a reseed does not make the demo flicker and
the unit tests are stable.

Kept as a pure builder so the seed handler and its unit tests share one source
of truth for the snapshot shape and values.
"""

from __future__ import annotations

import random
from typing import Any

# Canonical demo property used across PULSE/LUMI (see lumi seed-data and
# pulse.demo_simulator.simulator.DEMO_PROPERTY_ID). Its snapshot uses the exact
# curated prototype values; the other pilot properties derive their own.
DEMO_PROPERTY_ID = "ALOHA-CHI-001"

# Banquet service always "opens" this many minutes after the seed instant in the
# curated CHI snapshot (minutesRemaining=18). The handler recomputes the live
# remaining minutes at read time from ``serviceOpensAtEpoch``; this is only the
# seed-time fallback baseline.
_CHI_MINUTES_REMAINING = 18
_CHI_PROGRESS_PCT = 72

# Per-property flavor for the banquet title so each property reads distinctly.
# Falls back to a generic corporate banquet for any id not listed.
_BANQUET_EVENTS: dict[str, str] = {
    "ALOHA-CHI-001": "Meridian Corp Breakfast",
    "ALOHA-MIA-001": "Oceanview Wedding Brunch",
    "ALOHA-TYO-001": "Sakura Tech Summit Lunch",
    "ALOHA-MAD-001": "Ibérico Investors Gala",
    "ALOHA-BOM-001": "Monsoon Trade Expo Tea",
}


def _rng(property_id: str) -> random.Random:
    """Return a deterministic RNG seeded from the property id (pure per id).

    Using the property id as the seed makes every derived number stable for a
    given property, so a reseed is a no-op visually and tests are repeatable.

    Args:
        property_id: The property whose snapshot is being generated.

    Returns:
        A ``random.Random`` seeded deterministically from ``property_id``.
    """
    return random.Random(f"kitchen::{property_id}")


def _banquet_countdown(property_id: str, rng: random.Random) -> dict[str, Any]:
    """Build the active banquet countdown card for a property.

    ``ALOHA-CHI-001`` keeps the exact curated prototype values. Other properties
    get a distinct event name, cover count, and countdown derived from the RNG.
    ``serviceOpensAtEpoch`` is intentionally omitted here (it is a wall-clock
    field the read path stamps live); the seeded ``minutesRemaining`` /
    ``progressPct`` are the static fallback the handler overrides at read time.

    Args:
        property_id: The property the card belongs to.
        rng: Deterministic RNG for this property.

    Returns:
        The ``banquetCountdown`` sub-payload.
    """
    event = _BANQUET_EVENTS.get(property_id, "Corporate Breakfast")
    if property_id == DEMO_PROPERTY_ID:
        covers = 82
        minutes_remaining = _CHI_MINUTES_REMAINING
        progress_pct = _CHI_PROGRESS_PCT
    else:
        covers = rng.choice([46, 58, 64, 90, 110, 128])
        minutes_remaining = rng.choice([9, 14, 22, 27, 35, 42])
        # Progress inversely tracks how close service is (more elapsed -> higher).
        progress_pct = max(20, min(95, 100 - minutes_remaining * 2))
    badge = "On Track" if minutes_remaining >= 12 else "Tight"
    return {
        "title": f"{event} \u00b7 {covers} Covers",
        "badge": badge,
        "minutesRemaining": minutes_remaining,
        "progressPct": progress_pct,
        "subline": (
            f"Service opens after countdown \u00b7 Kitchen dispatch in "
            f"{minutes_remaining} min"
        ),
    }


def _fb_stats(property_id: str, rng: random.Random) -> list[dict[str, Any]]:
    """Build today's F&B summary tiles (orders / avg ticket / accuracy).

    Args:
        property_id: The property the tiles belong to.
        rng: Deterministic RNG for this property.

    Returns:
        The ``fbStats`` list (three tiles).
    """
    if property_id == DEMO_PROPERTY_ID:
        orders, in_flight, avg_ticket, baseline, accuracy = 47, 12, 38, 32, 94
    else:
        orders = rng.randint(28, 72)
        in_flight = rng.randint(4, 18)
        avg_ticket = rng.choice([29, 34, 41, 45, 52])
        baseline = avg_ticket - rng.choice([3, 5, 6, 8])
        accuracy = rng.randint(90, 98)
    accuracy_tone = "success" if accuracy >= 95 else "warning"
    return [
        {
            "label": "Orders",
            "value": str(orders),
            "delta": f"In-flight: {in_flight}",
            "deltaTone": "success",
        },
        {
            "label": "Avg Ticket",
            "value": f"${avg_ticket}",
            "delta": f"up vs ${baseline} avg",
            "deltaTone": "success",
        },
        {
            "label": "Accuracy",
            "value": f"{accuracy}%",
            "delta": "Target >=95%",
            "deltaTone": accuracy_tone,
        },
    ]


def _delivery_sla(property_id: str, rng: random.Random) -> dict[str, Any]:
    """Build the room-service delivery SLA tracker for a property.

    Args:
        property_id: The property the tracker belongs to.
        rng: Deterministic RNG for this property.

    Returns:
        The ``deliverySla`` sub-payload.
    """
    if property_id == DEMO_PROPERTY_ID:
        pct, avg_minutes, at_risk = 90, 27, 3
    else:
        pct = rng.randint(82, 98)
        avg_minutes = rng.randint(19, 33)
        at_risk = rng.randint(0, 5)
    return {
        "label": "Room Service SLA",
        "pct": pct,
        "avgLabel": f"{avg_minutes} min",
        "targetLabel": "30 min",
        "atRisk": at_risk,
        "standardLabel": "IHG Standard: 30 min",
    }


# Static in-flight order feed for the curated CHI snapshot (prototype-exact).
_CHI_KITCHEN_ORDERS: list[dict[str, Any]] = [
    {
        "id": "ko-1",
        "kind": "room-service",
        "title": "Room 1802 \u00b7 Room Service",
        "detail": "Eggs Benedict + OJ \u00b7 Sarah Reeves",
        "elapsedLabel": "31 min",
        "slaState": "breached",
        "slaLabel": "SLA breached",
    },
    {
        "id": "ko-2",
        "kind": "room-service",
        "title": "Room 2208 \u00b7 Room Service",
        "detail": "Continental breakfast \u00b7 2 pax",
        "elapsedLabel": "26 min",
        "slaState": "at-risk",
        "slaLabel": "At risk (4 min)",
    },
    {
        "id": "ko-3",
        "kind": "banquet",
        "title": "Meridian Corp \u00b7 Banquet Setup",
        "detail": "82 covers \u00b7 Ballroom A \u00b7 11:00 AM",
        "elapsedLabel": "18 min",
        "slaState": "on-time",
        "slaLabel": "On track",
    },
    {
        "id": "ko-4",
        "kind": "room-service",
        "title": "Room 0612 \u00b7 Room Service",
        "detail": "Club sandwich + fries",
        "elapsedLabel": "14 min",
        "slaState": "on-time",
        "slaLabel": "On track",
    },
    {
        "id": "ko-5",
        "kind": "external",
        "title": "External \u00b7 Lobby Bar Catering",
        "detail": "Corporate coffee service \u00b7 20 pax",
        "elapsedLabel": "8 min",
        "slaState": "on-time",
        "slaLabel": "On track",
    },
]

# Menu pool for deriving distinct room-service orders on non-CHI properties.
_ROOM_SERVICE_ITEMS: tuple[str, ...] = (
    "Avocado toast + latte",
    "Grilled salmon + greens",
    "Margherita pizza",
    "Wagyu burger + fries",
    "Fruit plate + espresso",
    "Chicken katsu curry",
    "Caprese salad + sparkling water",
    "Steak frites \u00b7 medium rare",
)


def _kitchen_orders(
    property_id: str, banquet_event: str, rng: random.Random
) -> list[dict[str, Any]]:
    """Build the in-flight order feed for a property.

    ``ALOHA-CHI-001`` returns the prototype-exact five orders. Other properties
    get a derived feed (one banquet setup tied to their event + several
    room-service orders) with plausible elapsed times and SLA states.

    Args:
        property_id: The property the feed belongs to.
        banquet_event: The property's banquet event name (for the banquet row).
        rng: Deterministic RNG for this property.

    Returns:
        The ``kitchenOrders`` list.
    """
    if property_id == DEMO_PROPERTY_ID:
        return [dict(order) for order in _CHI_KITCHEN_ORDERS]

    orders: list[dict[str, Any]] = []
    count = rng.randint(4, 6)
    for index in range(count):
        elapsed = rng.randint(4, 34)
        if elapsed > 30:
            sla_state, sla_label = "breached", "SLA breached"
        elif elapsed >= 26:
            sla_state, sla_label = "at-risk", f"At risk ({30 - elapsed} min)"
        else:
            sla_state, sla_label = "on-time", "On track"
        room = rng.randint(300, 2600)
        orders.append(
            {
                "id": f"ko-{index + 1}",
                "kind": "room-service",
                "title": f"Room {room:04d} \u00b7 Room Service",
                "detail": rng.choice(_ROOM_SERVICE_ITEMS),
                "elapsedLabel": f"{elapsed} min",
                "slaState": sla_state,
                "slaLabel": sla_label,
            }
        )
    # Prepend the property's banquet setup so the feed ties to the countdown.
    covers = rng.choice([46, 58, 64, 90, 110, 128])
    orders.insert(
        0,
        {
            "id": "ko-banquet",
            "kind": "banquet",
            "title": f"{banquet_event} \u00b7 Banquet Setup",
            "detail": f"{covers} covers \u00b7 Ballroom \u00b7 service soon",
            "elapsedLabel": f"{rng.randint(10, 25)} min",
            "slaState": "on-time",
            "slaLabel": "On track",
        },
    )
    return orders


def _channel_mix(
    property_id: str, rng: random.Random
) -> tuple[list[dict[str, Any]], str]:
    """Build the revenue channel mix slices and advisory note for a property.

    Args:
        property_id: The property the mix belongs to.
        rng: Deterministic RNG for this property.

    Returns:
        A ``(channelMix, channelMixNote)`` tuple. The 3rd-party slice is flagged
        ``warning`` when it exceeds the 3% target and the note quantifies the
        estimated commission leakage.
    """
    if property_id == DEMO_PROPERTY_ID:
        third_party = 5
        room_svc, banquet, bar = 62, 24, 9
    else:
        third_party = rng.randint(2, 9)
        bar = rng.randint(7, 14)
        banquet = rng.randint(18, 30)
        room_svc = 100 - third_party - bar - banquet
    over_target = third_party > 3
    channel_mix = [
        {"label": "Room Svc", "pct": room_svc},
        {"label": "Banquet", "pct": banquet},
        {"label": "Bar", "pct": bar},
        {"label": "3rd Party", "pct": third_party, "warning": over_target},
    ]
    if over_target:
        leakage = third_party * 25  # rough $/day proxy, scales with the slice.
        note = (
            f"3rd party at {third_party}% (target <3%) - commission leakage "
            f"~${leakage}/day. Consider an in-room QR ordering push."
        )
    else:
        note = (
            f"3rd party at {third_party}% (within <3% target). Direct channels "
            "healthy - maintain the in-room QR ordering push."
        )
    return channel_mix, note


def build_kitchen_snapshot(property_id: str) -> dict[str, Any]:
    """Build the pulse-kitchen item for a property (pure, deterministic).

    The snapshot is a single item keyed by ``propertyId`` holding the whole
    Kitchen tab payload as nested attributes. ``ALOHA-CHI-001`` yields the exact
    curated prototype values; every other property yields distinct-but-stable
    values derived deterministically from its id, so each demo GM sees fresh
    kitchen data and a reseed never changes the numbers for a given property.

    Args:
        property_id: The property the snapshot belongs to (partition key).

    Returns:
        A ready-to-put ``pulse-kitchen`` item with camelCase attributes.
    """
    rng = _rng(property_id)
    banquet_event = _BANQUET_EVENTS.get(property_id, "Corporate Breakfast")
    channel_mix, channel_mix_note = _channel_mix(property_id, rng)
    return {
        "propertyId": property_id,
        "banquetCountdown": _banquet_countdown(property_id, rng),
        "fbStats": _fb_stats(property_id, rng),
        "deliverySla": _delivery_sla(property_id, rng),
        "kitchenOrders": _kitchen_orders(property_id, banquet_event, rng),
        "channelMix": channel_mix,
        "channelMixNote": channel_mix_note,
    }


__all__ = ["DEMO_PROPERTY_ID", "build_kitchen_snapshot"]
