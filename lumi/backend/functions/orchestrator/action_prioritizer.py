"""LUMI Action Prioritizer - sorts action items by severity.

Implements the priority ordering for action items pulled from SPOG/MDP
sources. Actions are sorted by severity enum: URGENT > HIGH > MEDIUM > LOW,
ensuring the GM sees the most critical items first in their daily brief.
"""

from typing import Any, Dict, List

from aws_lambda_powertools import Logger

logger = Logger(service="stayos-orchestrator")

# Severity ranking: lower number = higher priority
SEVERITY_ORDER: Dict[str, int] = {
    "URGENT": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
}


def prioritize_actions(raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sort action items by severity in descending priority order.

    Takes the combined raw data from data_puller and returns the
    actionItems list sorted from most urgent to least urgent.
    Items with the same severity maintain their original order.

    Args:
        raw_data: Combined property data dict containing an
            "actionItems" key with a list of action item dicts.

    Returns:
        Sorted list of action item dicts, ordered URGENT > HIGH > MEDIUM > LOW.
        Returns an empty list if no action items are present.
    """
    action_items = raw_data.get("actionItems", [])

    if not action_items:
        logger.info("No action items to prioritize")
        return []

    sorted_items = sorted(
        action_items,
        key=lambda item: SEVERITY_ORDER.get(item.get("severity", "LOW"), 99),
    )

    logger.info(
        "Action items prioritized",
        total_items=len(sorted_items),
        urgent_count=sum(1 for item in sorted_items if item.get("severity") == "URGENT"),
        high_count=sum(1 for item in sorted_items if item.get("severity") == "HIGH"),
    )

    return sorted_items
