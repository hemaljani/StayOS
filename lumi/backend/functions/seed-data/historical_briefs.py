"""LUMI Historical Briefs Seeder - Generates 7 days of historical brief records.

Seeds the stayos-briefs DynamoDB table with realistic historical brief data for
all 5 pilot GMs. Each GM gets 7 daily records (35 total) with per-property KPI
variance, rotating action items, and locale-appropriate narrative text.

Runs as Step 4 in the seed-data custom resource handler, after Cognito users,
settings, and schedules are provisioned. Failure does not block CloudFormation
deployment (graceful degradation).

Supports REQ-HIST-1 through REQ-HIST-6.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

# Module-level logger matching existing seed-data service name
logger = Logger(service="stayos-seed-data")

# Module-level DynamoDB resource for connection reuse across invocations
_dynamodb_resource = boto3.resource("dynamodb")

# --- Constants ---

# TTL for seeded records: 30 days from creation (matches orchestrator)
TTL_DAYS: int = 30

# Default number of days to seed (today - 6 through today inclusive)
DEFAULT_DAYS: int = 7


# Per-property profiles with baseline KPIs and deterministic variance arrays.
# Each property has a unique data pattern (business weekday, leisure weekend,
# consistent, seasonal, growing) shaped by its occ_offsets and adr_offsets.
PROPERTY_PROFILES: Dict[str, Dict[str, Any]] = {
    # Chicago: Business weekday peak pattern - strong occupancy with upward trend
    "ALOHA-CHI-001": {
        "occupancy_base": 85,
        "occ_min": 83,
        "occ_max": 91,
        "adr_base": 245,
        "adr_min": 238,
        "adr_max": 258,
        "currency": "USD",
        "budget_occupancy": 83,
        "budget_adr": 240,
        "vip_min": 5,
        "vip_max": 8,
        "total_rooms": 368,
        "occ_offsets": [-2, 0, -1, 1, 3, 2, 4],
        "adr_offsets": [-5, -2, 0, 3, 5, 8, 10],
    },
    # Miami: Leisure weekend peak pattern - higher ADR, moderate occupancy
    "ALOHA-MIA-001": {
        "occupancy_base": 82,
        "occ_min": 78,
        "occ_max": 88,
        "adr_base": 278,
        "adr_min": 265,
        "adr_max": 295,
        "currency": "USD",
        "budget_occupancy": 80,
        "budget_adr": 270,
        "vip_min": 4,
        "vip_max": 7,
        "total_rooms": 425,
        "occ_offsets": [-3, -1, 0, -2, 2, 4, 3],
        "adr_offsets": [-8, -5, 0, -3, 5, 12, 10],
    },

    # Tokyo: Consistent low variance - high occupancy, stable ADR
    "ALOHA-TYO-001": {
        "occupancy_base": 91,
        "occ_min": 88,
        "occ_max": 96,
        "adr_base": 195,
        "adr_min": 185,
        "adr_max": 210,
        "currency": "JPY",
        "budget_occupancy": 90,
        "budget_adr": 190,
        "vip_min": 5,
        "vip_max": 8,
        "total_rooms": 480,
        "occ_offsets": [-1, 0, 1, -1, 2, 1, 3],
        "adr_offsets": [-3, -1, 0, 2, 3, 5, 8],
    },
    # Madrid: Seasonal mid-week dip pattern - moderate occupancy with recovery
    "ALOHA-MAD-001": {
        "occupancy_base": 78,
        "occ_min": 72,
        "occ_max": 85,
        "adr_base": 188,
        "adr_min": 175,
        "adr_max": 205,
        "currency": "EUR",
        "budget_occupancy": 76,
        "budget_adr": 180,
        "vip_min": 3,
        "vip_max": 6,
        "total_rooms": 380,
        "occ_offsets": [-4, -2, 0, -3, 1, 3, 4],
        "adr_offsets": [-8, -5, -2, 0, 5, 8, 12],
    },
    # Mumbai: Growing trend pattern - steadily improving occupancy
    "ALOHA-BOM-001": {
        "occupancy_base": 84,
        "occ_min": 80,
        "occ_max": 90,
        "adr_base": 138,
        "adr_min": 125,
        "adr_max": 155,
        "currency": "INR",
        "budget_occupancy": 82,
        "budget_adr": 130,
        "vip_min": 6,
        "vip_max": 9,
        "total_rooms": 355,
        "occ_offsets": [-3, -1, 0, 2, 1, 3, 4],
        "adr_offsets": [-5, -2, 0, 3, 5, 8, 12],
    },
}


# Action item templates pool - 8 items covering 5 canonical types from AI-PLC.
# Each template has eligibility rules that gate appearance based on KPI values.
ACTION_ITEM_POOL: List[Dict[str, Any]] = [
    {
        "type": "OVERBOOKING_RISK",
        "severity": "URGENT",
        "min_occupancy": 88,
        "title_template": "Overbooking Risk - +{overage} Rooms",
        "detail_template": "{confirmed} confirmed vs {available} available. Walk strategy required by 7 AM.",
        "source": "SPOG_XPMS",
    },
    {
        "type": "ROOMS_OUT_OF_ORDER",
        "severity": "URGENT",
        "title_template": "{count} Rooms Out of Order",
        "detail_template": "Maintenance issues reported. HotSOS work orders pending {hours} hrs.",
        "source": "SPOG_HOTSOS_GXP",
    },
    {
        "type": "VIP_ARRIVAL_ALERT",
        "severity": "HIGH",
        "requires_vip": True,
        "title_template": "VIP Arrival - {guest_name}",
        "detail_template": "Suite {room} - Arrives {time} - {stays} stays. Special preferences noted.",
        "source": "SPOG_LOYALTY_CRM",
    },
    {
        "type": "UPSELL_OPPORTUNITY",
        "severity": "MEDIUM",
        "min_occupancy": 80,
        "title_template": "Upsell Opportunity - {eligible} Eligible Arrivals",
        "detail_template": "{eligible} standard reservations eligible for suite upgrade. Avg upsell: +${value}/night.",
        "source": "SPOG_XPMS_REVENUE",
    },
    {
        "type": "STAFFING_CONFIRMED",
        "severity": "LOW",
        "title_template": "F&B Staffing Confirmed",
        "detail_template": "Full team confirmed for today's operations. All shifts covered.",
        "source": "SPOG_GXP",
    },
    {
        "type": "ROOMS_OUT_OF_ORDER",
        "severity": "HIGH",
        "title_template": "{count} Rooms Under Maintenance",
        "detail_template": "Scheduled maintenance on {count} rooms. Expected completion by 2 PM.",
        "source": "SPOG_HOTSOS_GXP",
    },
    {
        "type": "VIP_ARRIVAL_ALERT",
        "severity": "MEDIUM",
        "requires_vip": True,
        "title_template": "Titanium VIP - {guest_name}",
        "detail_template": "Room {room} - Arrives {time} - {stays} stays. Loyalty preferences on file.",
        "source": "SPOG_LOYALTY_CRM",
    },
    {
        "type": "UPSELL_OPPORTUNITY",
        "severity": "HIGH",
        "min_occupancy": 75,
        "title_template": "Premium Upsell - {eligible} Opportunities",
        "detail_template": "{eligible} arrivals qualify for premium floor upgrade. Revenue potential: ${value}.",
        "source": "SPOG_XPMS_REVENUE",
    },
]


# Rotating VIP guest names for action item hydration
_VIP_GUESTS: List[Dict[str, str]] = [
    {"name": "David Chen", "tier": "AMBASSADOR", "room": "2401", "stays": "47"},
    {"name": "Sarah Williams", "tier": "TITANIUM", "room": "1802", "stays": "31"},
    {"name": "Robert Kim", "tier": "AMBASSADOR", "room": "2205", "stays": "52"},
    {"name": "Elena Vasquez", "tier": "TITANIUM", "room": "1604", "stays": "28"},
    {"name": "James Park", "tier": "AMBASSADOR", "room": "2301", "stays": "39"},
    {"name": "Maria Santos", "tier": "TITANIUM", "room": "1901", "stays": "22"},
    {"name": "Thomas Mueller", "tier": "AMBASSADOR", "room": "2102", "stays": "44"},
]

# Arrival time rotation for VIP action items
_VIP_ARRIVAL_TIMES: List[str] = [
    "2:00 PM", "3:30 PM", "1:00 PM", "4:00 PM", "11:00 AM", "5:00 PM", "12:30 PM",
]


# Narrative templates per language - 3 variants each, rotated by day_index % 3.
# Each template is ~150 words with placeholders for KPI values and context.
NARRATIVE_TEMPLATES: Dict[str, List[str]] = {
    "en-US": [
        (
            "Good morning, {gm_first_name}. Here's your {property_name} update for "
            "{date}. Occupancy is at {occ}% with ADR at {currency}{adr}, giving us a "
            "RevPAR of {currency}{revpar}. We have {vip_count} VIP arrivals today. "
            "Your top priority: {top_action}. Overall, we're tracking ahead of budget "
            "on rate and occupancy is showing positive momentum compared to last week. "
            "The team is prepared for today's operations. I'll flag anything that needs "
            "your immediate attention throughout the day."
        ),
        (
            "Morning, {gm_first_name}. {property_name} daily brief for {date}. "
            "Current occupancy stands at {occ}%, ADR is {currency}{adr}, and RevPAR "
            "is {currency}{revpar}. Today brings {vip_count} VIP guests. Key focus: "
            "{top_action}. The revenue pace continues to look healthy with strong "
            "demand signals. Operations are running smoothly and the front desk team "
            "is briefed on all special arrivals. Let me know if you need deeper "
            "analysis on any metric."
        ),
        (
            "{gm_first_name}, here's what you need to know for {date} at "
            "{property_name}. We're running at {occ}% occupancy with an ADR of "
            "{currency}{adr} and RevPAR at {currency}{revpar}. Expecting {vip_count} "
            "VIP arrivals. Most important item: {top_action}. Week-over-week trends "
            "remain positive and the property is well-positioned. All departments "
            "are staffed appropriately for today's volume. Reach out if you'd like "
            "me to drill into any specific area."
        ),
    ],

    "es-ES": [
        (
            "Buenos dias, {gm_first_name}. Aqui esta tu resumen de {property_name} "
            "para {date}. La ocupacion esta al {occ}% con un ADR de {currency}{adr}, "
            "lo que nos da un RevPAR de {currency}{revpar}. Tenemos {vip_count} "
            "llegadas VIP hoy. Tu prioridad principal: {top_action}. En general, "
            "estamos por encima del presupuesto en tarifa y la ocupacion muestra "
            "impulso positivo respecto a la semana pasada. El equipo esta preparado "
            "para las operaciones de hoy."
        ),
        (
            "Buen dia, {gm_first_name}. Resumen diario de {property_name} para "
            "{date}. Ocupacion actual al {occ}%, ADR de {currency}{adr} y RevPAR "
            "de {currency}{revpar}. Hoy recibimos {vip_count} huespedes VIP. "
            "Enfoque clave: {top_action}. El ritmo de ingresos sigue saludable con "
            "senales de demanda fuertes. Las operaciones funcionan sin problemas y "
            "el equipo de recepcion esta informado sobre todas las llegadas "
            "especiales."
        ),
        (
            "{gm_first_name}, esto es lo que necesitas saber para {date} en "
            "{property_name}. Estamos al {occ}% de ocupacion con un ADR de "
            "{currency}{adr} y RevPAR de {currency}{revpar}. Esperamos {vip_count} "
            "llegadas VIP. Elemento mas importante: {top_action}. Las tendencias "
            "semanales siguen positivas y la propiedad esta bien posicionada. "
            "Todos los departamentos cuentan con personal adecuado para el volumen "
            "de hoy."
        ),
    ],

    "ja-JP": [
        (
            "おはようございます、{gm_first_name}さん。{date}の{property_name}の"
            "アップデートです。稼働率は{occ}%、ADRは{currency}{adr}、RevPARは"
            "{currency}{revpar}です。本日{vip_count}名のVIPゲストが到着予定です。"
            "最優先事項：{top_action}。全体的に予算を上回るペースで推移しており、"
            "先週比でもポジティブな勢いを示しています。チームは本日の運営に"
            "向けて準備完了しています。"
        ),
        (
            "{gm_first_name}さん、{date}の{property_name}デイリーブリーフです。"
            "現在の稼働率は{occ}%、ADRは{currency}{adr}、RevPARは"
            "{currency}{revpar}です。本日のVIP到着は{vip_count}名です。"
            "重要ポイント：{top_action}。収益ペースは引き続き好調で、需要"
            "シグナルも強く出ています。フロントチームは全ての特別到着について"
            "ブリーフィング済みです。"
        ),
        (
            "{gm_first_name}さん、{date}の{property_name}について重要な情報です。"
            "稼働率{occ}%、ADR {currency}{adr}、RevPAR {currency}{revpar}で"
            "運営中です。VIP到着{vip_count}名を予定しています。最重要項目："
            "{top_action}。週次トレンドはポジティブを維持し、プロパティは"
            "良好なポジションにあります。本日のボリュームに対して全部門の"
            "人員配置は適切です。"
        ),
    ],
}



# --- Internal Functions: KPI Generation (Task 3) ---


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between minimum and maximum bounds.

    Args:
        value: The value to clamp.
        min_val: Lower bound (inclusive).
        max_val: Upper bound (inclusive).

    Returns:
        The clamped value within [min_val, max_val].
    """
    return max(min_val, min(value, max_val))


def _convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB's boto3 resource layer requires Decimal types for numeric values
    instead of Python floats. This function walks the nested dict/list structure
    and replaces all float instances with their Decimal equivalent.

    Args:
        obj: Any value - dict, list, float, int, str, None, or bool.

    Returns:
        The same structure with all float values replaced by Decimal.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_floats_to_decimal(item) for item in obj]
    return obj


def _compute_vs_last_week(current: float, baseline: float) -> float:
    """Calculate the delta between current and baseline values.

    Args:
        current: Current day's value.
        baseline: Prior week's value (or synthetic baseline).

    Returns:
        Rounded delta (current - baseline) to 1 decimal place.
    """
    return round(current - baseline, 1)



def _generate_daily_kpis(
    profile: Dict[str, Any],
    day_index: int,
    brief_date: str,
) -> Dict[str, Any]:
    """Generate a complete dailyKPIs dict for one property on one day.

    Uses deterministic variance arrays from the property profile to produce
    realistic-looking KPI values without randomness. RevPAR is calculated
    from occupancy and ADR (not independently generated).

    Args:
        profile: Property profile dict from PROPERTY_PROFILES.
        day_index: Day position in the 7-day window (0 = oldest, 6 = today).
        brief_date: Date string in YYYY-MM-DD format.

    Returns:
        Complete dailyKPIs dict matching orchestrator schema.
    """
    # Calculate occupancy with bounded variance from offset array
    occupancy = int(_clamp(
        profile["occupancy_base"] + profile["occ_offsets"][day_index],
        profile["occ_min"],
        profile["occ_max"],
    ))

    # Calculate ADR with bounded variance from offset array
    adr = int(_clamp(
        profile["adr_base"] + profile["adr_offsets"][day_index],
        profile["adr_min"],
        profile["adr_max"],
    ))

    # RevPAR is derived from occupancy and ADR (not independent)
    revpar = round(occupancy * adr / 100)

    # "vs last week" occupancy delta: use budget_occupancy as synthetic baseline
    # for day 0, otherwise use previous day's offset to simulate week-over-week
    if day_index == 0:
        occ_vs_last_week = _compute_vs_last_week(
            occupancy, profile["budget_occupancy"]
        )
    else:
        # Compare to previous day's occupancy as simplified "vs last week"
        prev_occ = int(_clamp(
            profile["occupancy_base"] + profile["occ_offsets"][day_index - 1],
            profile["occ_min"],
            profile["occ_max"],
        ))
        occ_vs_last_week = _compute_vs_last_week(occupancy, prev_occ)

    # Budget comparison
    occ_vs_budget = round(occupancy - profile["budget_occupancy"], 1)

    # ADR deltas
    adr_vs_last_week = adr - profile["budget_adr"] + (day_index % 5)
    adr_vs_budget = adr - profile["budget_adr"]
    adr_pace_pct = round(adr / profile["budget_adr"] * 100)

    # Forecast: occupancy + small upward adjustment
    forecast_3pm = min(occupancy + 2 + (day_index % 3), profile["occ_max"] + 2)


    # VIP counts deterministically rotated by day_index
    vip_range = profile["vip_max"] - profile["vip_min"] + 1
    vip_count = profile["vip_min"] + (day_index % vip_range)
    ambassador_count = vip_count // 3
    titanium_count = vip_count - ambassador_count

    # Arrivals/departures based on 40% daily turnover ratio
    total_arrivals = round(profile["total_rooms"] * occupancy / 100 * 0.4)
    total_departures = total_arrivals - 5 + (day_index % 3)

    # Confirmed reservations: slightly above capacity when high occupancy
    confirmed_reservations = (
        round(profile["total_rooms"] * occupancy / 100) + (2 if occupancy > 88 else 0)
    )
    available_rooms = profile["total_rooms"]

    # RevPAR budget and YOY calculation
    revpar_budget = round(profile["budget_occupancy"] * profile["budget_adr"] / 100)
    revpar_vs_yoy = round((revpar - revpar_budget) / max(revpar_budget, 1) * 100, 1)

    # Assemble the KPI snapshot matching orchestrator schema
    as_of = f"{brief_date}T06:30:00+00:00"

    return {
        "date": brief_date,
        "asOf": as_of,
        "occupancy": {
            "current": occupancy,
            "unit": "percent",
            "vsLastWeek": occ_vs_last_week,
            "vsBudget": occ_vs_budget,
            "forecast3pm": forecast_3pm,
        },
        "adr": {
            "current": adr,
            "currency": profile["currency"],
            "vsLastWeek": adr_vs_last_week,
            "vsBudget": adr_vs_budget,
            "pacePctOfBudget": adr_pace_pct,
        },
        "revPAR": {
            "current": revpar,
            "currency": profile["currency"],
            "vsYOY": revpar_vs_yoy,
            "budget": revpar_budget,
        },
        "arrivals": {
            "total": total_arrivals,
            "vipCount": vip_count,
            "ambassadorCount": ambassador_count,
            "titaniumCount": titanium_count,
            "platinumCount": 0,
        },
        "departures": {
            "total": total_departures,
            "groupCheckouts": 1 if day_index % 3 == 0 else 0,
            "groupRooms": 60 + (day_index * 5) if day_index % 3 == 0 else 0,
        },
        "confirmedReservations": confirmed_reservations,
        "availableRooms": available_rooms,
    }



# --- Internal Functions: Action Items (Task 4) ---


def _get_chicago_today_actions(brief_date: str) -> List[Dict[str, Any]]:
    """Return the exact 5 action items matching data_puller.py mock for Chicago today.

    Ensures continuity with the current live demo by using identical action
    items for today's Chicago brief. These match _get_mock_data() output.

    Args:
        brief_date: Today's date in YYYY-MM-DD format.

    Returns:
        List of 5 action item dicts matching the orchestrator's mock data.
    """
    now_iso = f"{brief_date}T06:30:00+00:00"

    return [
        {
            "id": "action-001",
            "type": "OVERBOOKING_RISK",
            "severity": "URGENT",
            "title": "Overbooking Risk - +6 Rooms",
            "detail": "374 confirmed vs 368 available. Walk strategy required by 7 AM.",
            "data": {
                "confirmedCount": 374,
                "availableRooms": 368,
                "overage": 6,
                "walkStrategy": {
                    "compProperty1": {
                        "name": "Aloha Midway",
                        "propertyId": "ALOHA-MDW-001",
                        "availableRooms": 3,
                    },
                    "compProperty2": {
                        "name": "Aloha O'Hare",
                        "propertyId": "ALOHA-ORD-001",
                        "availableRooms": 8,
                    },
                },
            },
            "generatedAt": now_iso,
            "source": "SPOG_XPMS",
        },

        {
            "id": "action-002",
            "type": "ROOMS_OUT_OF_ORDER",
            "severity": "URGENT",
            "title": "4 Rooms Out of Order",
            "detail": (
                "Rm 1204 & 1206 (HVAC), 0814 (plumbing), 2101 (deep clean). "
                "HotSOS WO #4421 open 28 hrs."
            ),
            "data": {
                "roomCount": 4,
                "rooms": [
                    {
                        "roomNumber": "1204",
                        "issue": "HVAC failure",
                        "isPremium": True,
                        "view": "lake",
                        "workOrderId": "WO-4421",
                        "openHours": 28,
                    },
                    {
                        "roomNumber": "1206",
                        "issue": "HVAC failure",
                        "isPremium": True,
                        "view": "lake",
                        "workOrderId": "WO-4421",
                        "openHours": 28,
                    },
                    {
                        "roomNumber": "0814",
                        "issue": "Plumbing",
                        "isPremium": False,
                        "view": None,
                        "workOrderId": "WO-4398",
                        "openHours": 14,
                    },
                    {
                        "roomNumber": "2101",
                        "issue": "Deep clean",
                        "isPremium": False,
                        "view": None,
                        "workOrderId": "WO-4402",
                        "openHours": 6,
                    },
                ],
            },
            "generatedAt": now_iso,
            "source": "SPOG_HOTSOS_GXP",
        },

        {
            "id": "action-003",
            "type": "VIP_ARRIVAL_ALERT",
            "severity": "HIGH",
            "title": "Ambassador VIP - David Chen",
            "detail": (
                "Suite 2401 - Arrives 2:00 PM - 47 stays - Anniversary. "
                "Champagne on arrival, feather-free, high floor."
            ),
            "data": {
                "guestId": "ALH-MBR-00238471",
                "guestName": "David Chen",
                "loyaltyTier": "AMBASSADOR",
                "loyaltyNumber": "AH-7821034",
                "totalStays": 47,
                "roomNumber": "2401",
                "roomType": "SUITE",
                "specialOccasion": "ANNIVERSARY",
                "preferences": [
                    "HIGH_FLOOR",
                    "FEATHER_FREE_BEDDING",
                    "CHAMPAGNE_ARRIVAL",
                ],
            },
            "generatedAt": now_iso,
            "source": "SPOG_LOYALTY_CRM",
        },
        {
            "id": "action-004",
            "type": "UPSELL_OPPORTUNITY",
            "severity": "MEDIUM",
            "title": "Upsell Opportunity - 28 Eligible Arrivals",
            "detail": (
                "28 standard reservations eligible for suite upgrade. "
                "Avg upsell: +$85/night."
            ),
            "data": {
                "eligibleCount": 28,
                "avgUpsellValuePerNight": 85,
                "totalPotentialRevenue": 2380,
                "recommendation": "Brief front desk at 9 AM standup",
            },
            "generatedAt": now_iso,
            "source": "SPOG_XPMS_REVENUE",
        },
        {
            "id": "action-005",
            "type": "STAFFING_CONFIRMED",
            "severity": "LOW",
            "title": "F&B Staffing Confirmed",
            "detail": (
                "Full team for Meridian Corp group checkout (82 rooms). "
                "Breakfast extended to 11 AM."
            ),
            "data": {
                "groupName": "Meridian Corp",
                "groupRooms": 82,
                "breakfastExtendedUntil": "11:00",
                "staffingStatus": "CONFIRMED",
            },
            "generatedAt": now_iso,
            "source": "SPOG_GXP",
        },
    ]



def _hydrate_action_item(
    template: Dict[str, Any],
    profile: Dict[str, Any],
    occupancy: int,
    vip_count: int,
    brief_date: str,
    day_index: int,
    idx: int,
    property_id: str,
) -> Dict[str, Any]:
    """Fill an action item template with property-specific values.

    Interpolates template strings with calculated values based on the
    current day's KPI data and property profile.

    Args:
        template: Action item template from ACTION_ITEM_POOL.
        profile: Property profile dict.
        occupancy: Current day's occupancy percentage.
        vip_count: Number of VIP arrivals for the day.
        brief_date: Date string in YYYY-MM-DD format.
        day_index: Day position (0-6) in the seed window.
        idx: Index of this item in the day's action list (for ID generation).
        property_id: Property identifier for unique ID generation.

    Returns:
        Hydrated action item dict ready for DynamoDB storage.
    """
    now_iso = f"{brief_date}T06:30:00+00:00"
    # Generate a unique ID using property suffix, day index, and item index
    item_id = f"action-{property_id[-3:]}-{day_index}-{idx}"

    # Build the data dict based on action type
    data: Dict[str, Any] = {}
    title = template["title_template"]
    detail = template["detail_template"]

    if template["type"] == "OVERBOOKING_RISK":
        confirmed = round(profile["total_rooms"] * occupancy / 100) + 2
        overage = confirmed - profile["total_rooms"]
        title = title.format(overage=overage)
        detail = detail.format(
            confirmed=confirmed, available=profile["total_rooms"]
        )
        data = {
            "confirmedCount": confirmed,
            "availableRooms": profile["total_rooms"],
            "overage": overage,
        }

    elif template["type"] == "ROOMS_OUT_OF_ORDER":
        # Rotate room count between 2-5 based on day
        room_count = 2 + (day_index % 4)
        hours = 6 + (day_index * 4)
        title = title.format(count=room_count)
        detail = detail.format(count=room_count, hours=hours)
        data = {"roomCount": room_count, "openHours": hours}

    elif template["type"] == "VIP_ARRIVAL_ALERT":
        # Rotate through VIP guest pool
        guest = _VIP_GUESTS[(day_index + idx) % len(_VIP_GUESTS)]
        arrival_time = _VIP_ARRIVAL_TIMES[(day_index + idx) % len(_VIP_ARRIVAL_TIMES)]
        title = title.format(guest_name=guest["name"])
        detail = detail.format(
            room=guest["room"],
            time=arrival_time,
            stays=guest["stays"],
        )
        data = {
            "guestName": guest["name"],
            "loyaltyTier": guest["tier"],
            "roomNumber": guest["room"],
            "totalStays": int(guest["stays"]),
            "arrivalTime": arrival_time,
        }

    elif template["type"] == "UPSELL_OPPORTUNITY":
        # Eligible count based on arrivals and day rotation
        eligible = 15 + (day_index * 3)
        value = 65 + (day_index * 5)
        title = title.format(eligible=eligible)
        detail = detail.format(eligible=eligible, value=value)
        data = {
            "eligibleCount": eligible,
            "avgUpsellValuePerNight": value,
            "totalPotentialRevenue": eligible * value,
        }

    elif template["type"] == "STAFFING_CONFIRMED":
        # Staffing is straightforward - no dynamic values needed
        data = {"staffingStatus": "CONFIRMED", "allShiftsCovered": True}

    return {
        "id": item_id,
        "type": template["type"],
        "severity": template["severity"],
        "title": title,
        "detail": detail,
        "data": data,
        "generatedAt": now_iso,
        "source": template["source"],
    }



def _select_action_items(
    profile: Dict[str, Any],
    occupancy: int,
    vip_count: int,
    day_index: int,
    property_id: str,
    brief_date: str,
) -> List[Dict[str, Any]]:
    """Select and hydrate action items for one day based on KPI values.

    Filters the action pool by eligibility rules (min_occupancy, requires_vip)
    then rotates through eligible items based on day_index for variety.
    For Chicago on day 6 (today), returns exact mock data for demo continuity.

    Args:
        profile: Property profile dict from PROPERTY_PROFILES.
        occupancy: Current day's occupancy percentage.
        vip_count: Number of VIP arrivals for the day.
        day_index: Day position (0-6) in the seed window.
        property_id: Property identifier (e.g., "ALOHA-CHI-001").
        brief_date: Date string in YYYY-MM-DD format.

    Returns:
        List of 3-6 hydrated action item dicts.
    """
    # For Chicago today (day 6), use exact mock data from data_puller.py
    if day_index == 6 and property_id == "ALOHA-CHI-001":
        return _get_chicago_today_actions(brief_date)

    # Filter eligible items from the pool based on current KPI values
    eligible: List[Dict[str, Any]] = []
    for action in ACTION_ITEM_POOL:
        # Skip overbooking if occupancy is below threshold
        if action.get("min_occupancy") and occupancy < action["min_occupancy"]:
            continue
        # Skip VIP alerts if no VIPs arriving
        if action.get("requires_vip") and vip_count == 0:
            continue
        eligible.append(action)

    # Target 3-6 items per day, rotating by day_index
    target_count = 3 + (day_index % 4)

    # Rotate starting position for variety across days
    start = day_index % max(len(eligible), 1)
    selected = eligible[start:start + target_count]
    # Wrap around if we need more items
    if len(selected) < target_count:
        remaining = target_count - len(selected)
        selected += eligible[:remaining]

    # Hydrate each selected template with property-specific values
    hydrated: List[Dict[str, Any]] = []
    for idx, template in enumerate(selected[:target_count]):
        item = _hydrate_action_item(
            template=template,
            profile=profile,
            occupancy=occupancy,
            vip_count=vip_count,
            brief_date=brief_date,
            day_index=day_index,
            idx=idx,
            property_id=property_id,
        )
        hydrated.append(item)

    return hydrated



# --- Internal Functions: Narrative Generation (Task 5) ---


def _generate_narrative(
    profile: Dict[str, Any],
    gm: Dict[str, Any],
    kpis: Dict[str, Any],
    actions: List[Dict[str, Any]],
    day_index: int,
    brief_date: str,
) -> str:
    """Generate a narrative string for one day's brief using template interpolation.

    Selects a template based on day_index rotation and fills all placeholders
    with property-specific KPI values and context. No Bedrock calls - pure
    string formatting for speed and determinism.

    Args:
        profile: Property profile dict from PROPERTY_PROFILES.
        gm: GM data dict from GM_SEED_DATA.
        kpis: Generated dailyKPIs dict for this day.
        actions: List of action items for this day.
        day_index: Day position (0-6) for template rotation.
        brief_date: Date string in YYYY-MM-DD format.

    Returns:
        Filled narrative string (~150 words).
    """
    language = gm.get("language", "en-US")

    # Fall back to en-US if language not in templates
    templates = NARRATIVE_TEMPLATES.get(language, NARRATIVE_TEMPLATES["en-US"])

    # Rotate template selection across days
    template_index = day_index % len(templates)
    template = templates[template_index]

    # Extract first name from full GM name
    gm_first_name = gm["gmName"].split()[0]

    # Get the top action item title for the narrative summary
    top_action = actions[0]["title"] if actions else "No urgent items"

    # Fill all placeholders
    narrative = template.format(
        gm_first_name=gm_first_name,
        property_name=gm["propertyName"],
        date=brief_date,
        occ=kpis["occupancy"]["current"],
        adr=kpis["adr"]["current"],
        currency=profile["currency"],
        revpar=kpis["revPAR"]["current"],
        vip_count=kpis["arrivals"]["vipCount"],
        top_action=top_action,
    )

    return narrative



# --- Internal Functions: Record Builder (Task 6) ---


def _build_historical_brief_record(
    gm: Dict[str, Any],
    profile: Dict[str, Any],
    brief_date: str,
    day_index: int,
) -> Dict[str, Any]:
    """Assemble a complete DynamoDB brief record for one property on one day.

    Builds the full record matching the schema produced by _build_brief_record
    in the orchestrator. Includes property metadata, KPIs, action items,
    narrative, and audio placeholder.

    Args:
        gm: GM data dict from GM_SEED_DATA.
        profile: Property profile dict from PROPERTY_PROFILES.
        brief_date: Date string in YYYY-MM-DD format (sort key).
        day_index: Day position (0-6) in the seed window.

    Returns:
        Complete DynamoDB item dict ready for PutItem.
    """
    property_id = gm["propertyId"]
    gm_alias = gm["gmAlias"]
    generated_at = f"{brief_date}T06:30:00+00:00"

    # Generate day-specific KPIs
    kpis = _generate_daily_kpis(profile, day_index, brief_date)

    # Get VIP count from generated KPIs for action item selection
    vip_count = kpis["arrivals"]["vipCount"]
    occupancy = kpis["occupancy"]["current"]

    # Select and hydrate action items
    actions = _select_action_items(
        profile=profile,
        occupancy=occupancy,
        vip_count=vip_count,
        day_index=day_index,
        property_id=property_id,
        brief_date=brief_date,
    )

    # Generate narrative text
    narrative = _generate_narrative(
        profile=profile,
        gm=gm,
        kpis=kpis,
        actions=actions,
        day_index=day_index,
        brief_date=brief_date,
    )


    # Status: DELIVERED for historical days (0-5), GENERATED for today (6)
    status = "GENERATED" if day_index == 6 else "DELIVERED"

    # TTL: 30 days from now in epoch seconds
    ttl_epoch = int(
        (datetime.now(tz=timezone.utc) + timedelta(days=TTL_DAYS)).timestamp()
    )

    # Extract city and state from GM city field (e.g., "Chicago, IL" -> "Chicago", "IL")
    city_parts = gm["city"].split(", ")
    city = city_parts[0]
    state = city_parts[1] if len(city_parts) > 1 else None

    # Determine country from property ID pattern
    country_map = {
        "CHI": "US",
        "MIA": "US",
        "TYO": "JP",
        "MAD": "ES",
        "BOM": "IN",
    }
    property_code = property_id.split("-")[1]
    country = country_map.get(property_code, "US")

    # Build the complete record matching orchestrator schema
    record: Dict[str, Any] = {
        "propertyId": property_id,
        "briefDate": brief_date,
        "generatedAt": generated_at,
        "asOf": generated_at,
        "property": {
            "propertyId": property_id,
            "propertyName": gm["propertyName"],
            "brand": gm["brand"],
            "city": city,
            "state": state,
            "country": country,
            "timezone": gm["timezone"],
            "totalRooms": gm["totalRooms"],
            "gmAlias": gm_alias,
            "gmName": gm["gmName"],
            "briefDeliveryTime": "06:30",
        },
        "dailyKPIs": kpis,
        "actionItems": actions,
        "vipArrivals": [],
        "narrative": narrative,
        "audioBrief": {
            "briefId": f"brief-{gm_alias}-{brief_date}",
            "status": status,
            "durationSeconds": 90,
            "s3Key": f"briefs/{property_id}/{brief_date}.mp3",
            "cloudFrontUrl": None,
            "voiceId": "Matthew",
            "engine": "neural",
        },
        "dataSourceStatus": {"MOCK_SEED": "SUCCESS"},
        "gmAlias": gm_alias,
        "language": gm["language"],
        "status": status,
        "ttl": ttl_epoch,
    }

    return record



# --- Public Interface (Task 6) ---


def seed_historical_briefs(
    table_name: str,
    gm_list: List[Dict[str, Any]],
    days: int = DEFAULT_DAYS,
) -> int:
    """Seed historical brief records for all GMs into DynamoDB.

    Generates and writes `days` daily brief records per GM. Uses conditional
    PutItem to ensure idempotency - existing records are never overwritten.
    Errors on individual records do not halt the seeding process.

    Args:
        table_name: Name of the DynamoDB briefs table (stayos-briefs).
        gm_list: List of GM dictionaries from GM_SEED_DATA.
        days: Number of days to seed (default 7, today-6 through today).

    Returns:
        Number of records successfully written (excludes skipped duplicates).

    Raises:
        ClientError: Only for table-not-found scenarios (logged and re-raised
            to trigger graceful degradation in the caller).
    """
    table = _dynamodb_resource.Table(table_name)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    records_written = 0
    records_skipped = 0
    records_errored = 0

    logger.info(
        "Starting historical brief seeding",
        extra={
            "table_name": table_name,
            "gm_count": len(gm_list),
            "days": days,
            "today": today,
        },
    )

    for gm in gm_list:
        property_id = gm["propertyId"]

        # Look up the property profile; skip if not defined
        profile = PROPERTY_PROFILES.get(property_id)
        if not profile:
            logger.warning(
                "No profile found for property, skipping",
                extra={"property_id": property_id},
            )
            continue


        # Generate records for each day in the window (oldest first)
        for day_index in range(days):
            # day_index 0 = today - (days-1), day_index (days-1) = today
            day_offset = day_index - (days - 1)
            brief_date = (
                datetime.now(tz=timezone.utc) + timedelta(days=day_offset)
            ).strftime("%Y-%m-%d")

            # Build the complete record
            record = _build_historical_brief_record(
                gm=gm,
                profile=profile,
                brief_date=brief_date,
                day_index=day_index,
            )

            try:
                # Conditional write ensures idempotency - won't overwrite
                # records written by the orchestrator or prior seed runs
                # Convert any float values to Decimal for DynamoDB compatibility
                dynamodb_record = _convert_floats_to_decimal(record)
                table.put_item(
                    Item=dynamodb_record,
                    ConditionExpression=(
                        "attribute_not_exists(propertyId) "
                        "AND attribute_not_exists(briefDate)"
                    ),
                )
                records_written += 1

            except ClientError as error:
                error_code = error.response["Error"]["Code"]
                if error_code == "ConditionalCheckFailedException":
                    # Record already exists - expected on re-run, skip silently
                    records_skipped += 1
                    logger.info(
                        "Brief record already exists, skipping",
                        extra={
                            "property_id": property_id,
                            "brief_date": brief_date,
                        },
                    )
                else:
                    # Other DynamoDB error - log and continue to next record
                    records_errored += 1
                    logger.error(
                        "Failed to write brief record",
                        extra={
                            "property_id": property_id,
                            "brief_date": brief_date,
                            "error_code": error_code,
                            "error": str(error),
                        },
                    )

    # Log summary metrics for observability (REQ-HIST-4)
    logger.info(
        "Historical brief seeding complete",
        extra={
            "records_written": records_written,
            "records_skipped": records_skipped,
            "records_errored": records_errored,
            "total_expected": len(gm_list) * days,
        },
    )

    return records_written
