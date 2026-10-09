"""Map authoritative revenue records into current and historical brief KPIs.

This pure mapping is packaged with both Lambdas so neither can invent a second
KPI series or truncate rates while the conversational tools retain decimals.
"""

from typing import Any, Dict


def format_revenue_kpis(
    revenue: Dict[str, Any],
    date_str: str,
    as_of: str,
    vip_counts: Dict[str, int],
) -> Dict[str, Any]:
    """Keep measurements precise and retain inventory capacity as a count."""
    occupancy = float(revenue.get("occupancyPct", 0) or 0)
    adr = float(revenue.get("adr", 0) or 0)
    revpar = float(revenue.get("revpar", 0) or 0)
    vs_last_week = float(revenue.get("vsLastWeek", 0) or 0)
    vs_budget = float(revenue.get("vsBudget", 0) or 0)
    # The dataset has no separate intraday forecast or RevPAR budget. Preserve
    # the existing prototype mapping instead of inventing independent values.
    budget = (
        round(revpar * max(0, occupancy - vs_budget) / occupancy)
        if occupancy > 0
        else revpar
    )
    currency = revenue.get("currency", "USD")
    return {
        "date": date_str,
        "asOf": as_of,
        "occupancy": {
            "current": occupancy,
            "unit": "percent",
            "vsLastWeek": vs_last_week,
            "vsBudget": vs_budget,
            "forecast3pm": occupancy,
        },
        "adr": {
            "current": adr,
            "currency": currency,
            "vsLastWeek": vs_last_week,
            "vsBudget": vs_budget,
            "pacePctOfBudget": max(0, round(100 + vs_budget)),
        },
        "revPAR": {
            "current": revpar,
            "currency": currency,
            "vsYOY": float(revenue.get("vsYOY", 0) or 0),
            "budget": budget,
        },
        "arrivals": {"total": int(revenue.get("arrivals", 0) or 0), **vip_counts},
        "departures": {
            "total": int(revenue.get("departures", 0) or 0),
            "groupCheckouts": 0,
            "groupRooms": 0,
        },
        "confirmedReservations": int(revenue.get("confirmedReservations", 0) or 0),
        # Legacy name used by the brief and oversell checks; it means inventory,
        # not remaining vacancy. Conversational tools call this totalRooms.
        "availableRooms": int(
            revenue.get("totalRooms", revenue.get("availableRooms", 0)) or 0
        ),
    }
