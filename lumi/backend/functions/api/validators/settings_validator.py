"""LUMI API settings input validator.

Validates GM settings update requests against business rules before
persisting to DynamoDB. Returns a list of validation errors (empty = valid).
"""

import re
from typing import Any, Dict, List

# Valid values for constrained fields
VALID_LANGUAGES = {"en-US", "es-ES", "ja-JP", "zh-CN"}
VALID_BRIEF_LENGTHS = {"brief", "standard", "detailed"}
VALID_ALERT_TOGGLE_KEYS = {
    "overbookingRisk",
    "roomsOutOfOrder",
    "vipArrivalAlert",
    "upsellOpportunity",
    "staffingConfirmed",
}

# HH:MM 24-hour format regex
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _coerce_integer_threshold(value: Any) -> int:
    """Coerce a KPI threshold to an int, rejecting non-integral values.

    KPI thresholds (e.g. occupancyAlertBelow, adrAlertBelow) are integers.
    ``int(70.5)`` would silently truncate to ``70`` and accept a fractional
    input, so a fractional numeric value must be rejected rather than coerced
    (review finding F-2). Accepts int, integral float (``70.0``), Decimal, and
    integral numeric string (``"70"``); rejects fractional values (``70.5``,
    ``"70.5"``), booleans, and non-numeric input.

    Args:
        value: The raw threshold value from the request body.

    Returns:
        The value as an int.

    Raises:
        ValueError: If the value is not an integral number.
        TypeError: If the value is a type that cannot be a numeric threshold
            (e.g. bool, None, list).
    """
    # bool is a subtype of int; reject it explicitly - True/False is not a
    # meaningful threshold and int(True) == 1 would otherwise slip through.
    if isinstance(value, bool):
        raise TypeError("threshold must be a number, not a boolean")

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("threshold must be a whole number")
        return int(value)

    # Decimal and numeric strings: parse via float to detect fractional parts,
    # then confirm integrality. This keeps the documented "accepts Decimal or
    # numeric string" behavior while still rejecting fractional inputs.
    numeric = float(value)  # raises TypeError/ValueError on non-numeric input
    if not numeric.is_integer():
        raise ValueError("threshold must be a whole number")
    return int(numeric)


def validate_settings(body: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate a settings update request body.

    Checks all provided fields against business rules. Only validates
    fields that are present in the body (partial updates are allowed).

    Args:
        body: Request body dictionary with settings fields to validate.

    Returns:
        List of validation error dictionaries, each with 'field' and 'message'.
        Empty list means validation passed.
    """
    errors: List[Dict[str, str]] = []

    # Validate briefDeliveryTime: HH:MM format
    if "briefDeliveryTime" in body:
        delivery_time = body["briefDeliveryTime"]
        if not isinstance(delivery_time, str) or not TIME_PATTERN.match(delivery_time):
            errors.append({
                "field": "briefDeliveryTime",
                "message": "briefDeliveryTime must be in HH:MM format (00:00-23:59)",
            })

    # Validate alertToggles: known keys with boolean values
    if "alertToggles" in body:
        toggles = body["alertToggles"]
        if not isinstance(toggles, dict):
            errors.append({
                "field": "alertToggles",
                "message": "alertToggles must be an object",
            })
        else:
            # Check for unknown keys
            unknown_keys = set(toggles.keys()) - VALID_ALERT_TOGGLE_KEYS
            if unknown_keys:
                errors.append({
                    "field": "alertToggles",
                    "message": f"Unknown alert toggle keys: {', '.join(sorted(unknown_keys))}",
                })
            # Check all values are boolean
            for key, value in toggles.items():
                if key in VALID_ALERT_TOGGLE_KEYS and not isinstance(value, bool):
                    errors.append({
                        "field": f"alertToggles.{key}",
                        "message": f"alertToggles.{key} must be a boolean",
                    })

    # Validate kpiThresholds
    if "kpiThresholds" in body:
        thresholds = body["kpiThresholds"]
        if not isinstance(thresholds, dict):
            errors.append({
                "field": "kpiThresholds",
                "message": "kpiThresholds must be an object",
            })
        else:
            # occupancyAlertBelow: integer 0-100 (accepts int, integral float,
            # Decimal, or numeric string; fractional values are rejected).
            if "occupancyAlertBelow" in thresholds:
                occ_val = thresholds["occupancyAlertBelow"]
                try:
                    occ_int = _coerce_integer_threshold(occ_val)
                    if occ_int < 0 or occ_int > 100:
                        errors.append({
                            "field": "kpiThresholds.occupancyAlertBelow",
                            "message": "occupancyAlertBelow must be an integer between 0 and 100",
                        })
                except (TypeError, ValueError):
                    errors.append({
                        "field": "kpiThresholds.occupancyAlertBelow",
                        "message": "occupancyAlertBelow must be an integer between 0 and 100",
                    })

            # adrAlertBelow: integer 0-1000 (accepts int, integral float,
            # Decimal, or numeric string; fractional values are rejected).
            if "adrAlertBelow" in thresholds:
                adr_val = thresholds["adrAlertBelow"]
                try:
                    adr_int = _coerce_integer_threshold(adr_val)
                    if adr_int < 0 or adr_int > 1000:
                        errors.append({
                            "field": "kpiThresholds.adrAlertBelow",
                            "message": "adrAlertBelow must be an integer between 0 and 1000",
                        })
                except (TypeError, ValueError):
                    errors.append({
                        "field": "kpiThresholds.adrAlertBelow",
                        "message": "adrAlertBelow must be an integer between 0 and 1000",
                    })

    # Validate audioPreferences
    if "audioPreferences" in body:
        audio_prefs = body["audioPreferences"]
        if not isinstance(audio_prefs, dict):
            errors.append({
                "field": "audioPreferences",
                "message": "audioPreferences must be an object",
            })
        else:
            # language: must be one of the supported languages
            if "language" in audio_prefs:
                lang = audio_prefs["language"]
                if lang not in VALID_LANGUAGES:
                    errors.append({
                        "field": "audioPreferences.language",
                        "message": f"language must be one of: {', '.join(sorted(VALID_LANGUAGES))}",
                    })

            # briefLength: must be one of brief, standard, detailed
            if "briefLength" in audio_prefs:
                length = audio_prefs["briefLength"]
                if length not in VALID_BRIEF_LENGTHS:
                    errors.append({
                        "field": "audioPreferences.briefLength",
                        "message": f"briefLength must be one of: {', '.join(sorted(VALID_BRIEF_LENGTHS))}",
                    })

    return errors
