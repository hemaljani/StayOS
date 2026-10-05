"""LUMI Data Validator - cross-checks AI-generated narrative against source data.

Extracts numeric values from the Bedrock-generated narrative and validates
each one against the source KPI data. This prevents hallucinated numbers
from reaching GMs, satisfying REQ-14's requirement for data accuracy.

Tolerance of +/- 1 accounts for rounding differences between source data
(which may be fractional) and the narrative (which typically uses integers).
"""

import re
from typing import Any, Dict, List, Tuple

from aws_lambda_powertools import Logger

logger = Logger(service="stayos-orchestrator")

# Tolerance for numeric comparison (accounts for rounding)
NUMERIC_TOLERANCE = 1.0

# Regex pattern to extract integers and decimals from narrative text
# Matches patterns like: 87, 4.2, 248, 103, 7.1
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


def validate_narrative(
    narrative: str, source_data: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """Validate that all numbers in the narrative match source KPI data.

    Extracts every numeric value from the narrative text and checks
    each one against the known source data values within a tolerance
    of +/- 1. This catches AI hallucination of statistics.

    Args:
        narrative: The AI-generated narrative text to validate.
        source_data: Combined property data dict containing dailyKPIs,
            actionItems, and vipArrivals with the authoritative values.

    Returns:
        A tuple of (is_valid, discrepancies) where:
        - is_valid: True if all numbers match source data, False otherwise.
        - discrepancies: List of human-readable discrepancy descriptions.
            Empty list when is_valid is True.
    """
    if not narrative or not narrative.strip():
        logger.info("Empty narrative - validation passes (no numbers to check)")
        return (True, [])

    # Extract all numbers from the narrative
    extracted_numbers = _extract_numbers(narrative)

    if not extracted_numbers:
        logger.info("No numbers found in narrative - validation passes")
        return (True, [])

    # Build the set of known source values for cross-reference
    known_values = _extract_source_values(source_data)

    logger.info(
        "Validating narrative numbers against source data",
        extracted_count=len(extracted_numbers),
        known_values_count=len(known_values),
    )

    # Check each extracted number against known values
    discrepancies: List[str] = []

    for number in extracted_numbers:
        if not _number_matches_source(number, known_values):
            discrepancies.append(f"{number} not found in source data")

    if discrepancies:
        logger.warning(
            "Narrative validation failed - discrepancies found",
            discrepancy_count=len(discrepancies),
            discrepancies=discrepancies,
        )
        return (False, discrepancies)

    logger.info("Narrative validation passed - all numbers match source data")
    return (True, [])


def _extract_numbers(text: str) -> List[float]:
    """Extract all numeric values from narrative text.

    Finds integers and decimal numbers using regex. Returns them as
    float values for consistent comparison.

    Args:
        text: The narrative text to extract numbers from.

    Returns:
        List of float values found in the text.
    """
    matches = _NUMBER_PATTERN.findall(text)
    return [float(match) for match in matches]


def _extract_source_values(source_data: Dict[str, Any]) -> List[float]:
    """Build a flat list of all known numeric values from source data.

    Recursively traverses the source data structure to collect every
    numeric value that the AI might reference in its narrative. This
    includes KPI values, counts, percentages, and room numbers.

    Args:
        source_data: Combined property data dict from data_puller.

    Returns:
        Flat list of all numeric values found in the source data.
    """
    values: List[float] = []
    _collect_numeric_values(source_data, values)
    return values


def _collect_numeric_values(data: Any, values: List[float]) -> None:
    """Recursively collect all numeric values from a nested data structure.

    Traverses dicts, lists, and scalar values to find every number
    in the source data tree.

    Args:
        data: Current node in the data structure (dict, list, or scalar).
        values: Accumulator list for discovered numeric values (modified in-place).
    """
    if isinstance(data, (int, float)):
        values.append(float(data))
    elif isinstance(data, dict):
        for value in data.values():
            _collect_numeric_values(value, values)
    elif isinstance(data, list):
        for item in data:
            _collect_numeric_values(item, values)


def _number_matches_source(number: float, known_values: List[float]) -> bool:
    """Check if a number matches any known source value within tolerance.

    A match occurs when the absolute difference between the extracted
    number and any known value is within NUMERIC_TOLERANCE (+/- 1).
    This accounts for rounding (e.g., 87 matches source value 87.4).

    Args:
        number: The extracted number from the narrative.
        known_values: List of all known numeric values from source data.

    Returns:
        True if the number matches at least one known value within tolerance.
    """
    for known in known_values:
        if abs(number - known) <= NUMERIC_TOLERANCE:
            return True
    return False
