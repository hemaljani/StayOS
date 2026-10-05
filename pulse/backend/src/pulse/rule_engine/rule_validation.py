"""Rule-definition update validation and default rule templates.

Property Admins edit rule definitions without a code deployment (Requirement
2). This module validates a submitted update before it is persisted and
provides the default rule templates seeded at launch.

Validation contract (Requirements 2.5, 2.6, 6.4):
    * An update is accepted **iff** every required attribute is present and
      every attribute value is within its permitted range. In particular
      ``escalationTimeoutSec`` must be in ``[60, 86400]`` (Requirement 2.6),
      ``tier`` must be a valid :class:`AlertTier`, ``ruleType`` a valid
      :class:`AlertType`, and the boolean flags actual booleans.
    * On rejection the previously stored definition is returned unchanged and
      the first invalid (or missing-required) attribute is identified, so the
      caller can retain the prior rule and surface which attribute failed.

The validation logic is pure (no I/O): it takes the submitted attributes and
the prior definition and returns a :class:`ValidationResult`. Persisting the
accepted rule and returning the API error are the caller's concern.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from pulse.common.models import (
    AlertTier,
    AlertType,
    RuleDefinition,
    TriggerCondition,
)

# Permitted range for the stored escalation timeout, in seconds (Requirement
# 2.6). The time-based chain further constrains the effective minutes to 1-60
# at runtime (Requirement 6.4), but the stored value is validated to this
# wider seconds range here.
ESCALATION_TIMEOUT_MIN_SEC = 60
ESCALATION_TIMEOUT_MAX_SEC = 86400

# Attributes that must be present on every rule-definition update.
REQUIRED_ATTRIBUTES = (
    "propertyId",
    "ruleType",
    "tier",
    "triggerCondition",
    "agentTriageEnabled",
    "escalationTimeoutSec",
    "enabled",
)

# Default tier per alert type (design "pulse-rules" model / Requirements 3-9).
_DEFAULT_TIER_BY_TYPE: dict[AlertType, AlertTier] = {
    AlertType.WALK_RISK: AlertTier.CRITICAL,
    AlertType.VIP_ROOM_NOT_READY: AlertTier.CRITICAL,
    AlertType.COMPLAINT_ESCALATION: AlertTier.CRITICAL,
    AlertType.OOO_CLUSTER: AlertTier.WARNING,
    AlertType.PREMIUM_CANCELLATION: AlertTier.INFO,
    AlertType.VIP_CHECKIN: AlertTier.INFO,
}

# Default declarative trigger condition and parameters per alert type. These
# mirror the operands each evaluator resolves (see evaluators.py).
_DEFAULT_TRIGGER_BY_TYPE: dict[AlertType, TriggerCondition] = {
    AlertType.WALK_RISK: {
        "operator": "gt",
        "left": "reservations.confirmed",
        "right": "rooms.available",
    },
    AlertType.VIP_ROOM_NOT_READY: {
        "operator": "lte",
        "left": "guest.etaMinutes",
        "right": "arrivalThresholdMin",
    },
    AlertType.COMPLAINT_ESCALATION: {
        "operator": "eq",
        "left": "complaint.escalationFlag",
        "right": True,
    },
    AlertType.OOO_CLUSTER: {
        "operator": "overlaps",
        "left": "ooo.range",
        "right": "block.range",
    },
    AlertType.PREMIUM_CANCELLATION: {
        "operator": "eq",
        "left": "reservation.isPremium",
        "right": True,
    },
    AlertType.VIP_CHECKIN: {
        "operator": "eq",
        "left": "guest.checkInRecorded",
        "right": True,
    },
}

_DEFAULT_PARAMETERS_BY_TYPE: dict[AlertType, dict[str, Any]] = {
    AlertType.WALK_RISK: {"loyaltyProtectionTier": "Gold"},
    AlertType.VIP_ROOM_NOT_READY: {"arrivalThresholdMin": 60},
    AlertType.COMPLAINT_ESCALATION: {},
    AlertType.OOO_CLUSTER: {"minClusterSize": 3},
    AlertType.PREMIUM_CANCELLATION: {},
    AlertType.VIP_CHECKIN: {},
}

# Default escalation timeout for a seeded template, in seconds (5 minutes).
DEFAULT_ESCALATION_TIMEOUT_SEC = 300


@dataclass
class ValidationResult:
    """Outcome of validating a rule-definition update.

    Attributes:
        accepted: Whether the update passed validation.
        rule: The resulting :class:`RuleDefinition`. On acceptance this is the
            new, validated definition; on rejection it is the prior definition
            unchanged (or ``None`` when there was no prior definition).
        invalid_attribute: The name of the first attribute that failed
            validation, or ``None`` when accepted.
        message: Human-readable explanation, or ``None`` when accepted.
    """

    accepted: bool
    rule: Optional[RuleDefinition]
    invalid_attribute: Optional[str] = None
    message: Optional[str] = None


def _reject(
    attribute: str, message: str, prior: Optional[RuleDefinition]
) -> ValidationResult:
    """Build a rejection result that retains the prior definition.

    Args:
        attribute: The offending attribute name.
        message: Human-readable reason for the rejection.
        prior: The previously stored definition, returned unchanged.

    Returns:
        A rejected :class:`ValidationResult`.
    """
    return ValidationResult(
        accepted=False,
        rule=prior,
        invalid_attribute=attribute,
        message=message,
    )


def _coerce_int(value: Any) -> Optional[int]:
    """Coerce a numeric value to ``int`` without accepting booleans.

    Args:
        value: The candidate value (``int`` or ``Decimal``).

    Returns:
        The integer value, or ``None`` if it is not an integer type.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return int(value)
    return None


def validate_rule_update(
    update: Mapping[str, Any], prior: Optional[RuleDefinition] = None
) -> ValidationResult:
    """Validate a submitted rule-definition update.

    Accepts the update only if every required attribute is present and every
    value is within its permitted range (Requirements 2.5, 2.6, 6.4). On the
    first failure it returns a rejection identifying the attribute and retains
    the prior definition unchanged.

    Args:
        update: The submitted attributes (camelCase keys, as received by the
            API).
        prior: The previously stored rule definition, returned unchanged on
            rejection.

    Returns:
        A :class:`ValidationResult`: accepted with the new definition, or
        rejected with the offending attribute and the prior definition.
    """
    # Required-presence check (Requirement 2.5: absent-when-required rejects).
    for attribute in REQUIRED_ATTRIBUTES:
        if update.get(attribute) is None:
            return _reject(
                attribute, f"Required attribute {attribute!r} is missing", prior
            )

    # ruleType must be a known alert type.
    try:
        rule_type = AlertType(update["ruleType"])
    except ValueError:
        return _reject("ruleType", f"Unknown ruleType {update['ruleType']!r}", prior)

    # tier must be a valid alert tier.
    try:
        tier = AlertTier(update["tier"])
    except ValueError:
        return _reject("tier", f"Unknown tier {update['tier']!r}", prior)

    # Boolean flags must be real booleans (not truthy strings/ints).
    for flag in ("agentTriageEnabled", "enabled"):
        if not isinstance(update[flag], bool):
            return _reject(flag, f"Attribute {flag!r} must be a boolean", prior)

    # triggerCondition must be a mapping.
    if not isinstance(update["triggerCondition"], Mapping):
        return _reject("triggerCondition", "triggerCondition must be an object", prior)

    # escalationTimeoutSec must be an integer within [60, 86400] (Req 2.6).
    timeout = _coerce_int(update["escalationTimeoutSec"])
    if timeout is None:
        return _reject(
            "escalationTimeoutSec",
            "escalationTimeoutSec must be an integer",
            prior,
        )
    if not (ESCALATION_TIMEOUT_MIN_SEC <= timeout <= ESCALATION_TIMEOUT_MAX_SEC):
        return _reject(
            "escalationTimeoutSec",
            (
                "escalationTimeoutSec must be between "
                f"{ESCALATION_TIMEOUT_MIN_SEC} and {ESCALATION_TIMEOUT_MAX_SEC} "
                "seconds inclusive"
            ),
            prior,
        )

    # parameters, when present, must be a mapping.
    parameters = update.get("parameters", {})
    if not isinstance(parameters, Mapping):
        return _reject("parameters", "parameters must be an object", prior)

    validated = RuleDefinition(
        property_id=update["propertyId"],
        rule_type=rule_type,
        tier=tier,
        trigger_condition=dict(update["triggerCondition"]),
        agent_triage_enabled=bool(update["agentTriageEnabled"]),
        escalation_timeout_sec=timeout,
        enabled=bool(update["enabled"]),
        parameters=dict(parameters),
        updated_at=update.get("updatedAt"),
        updated_by=update.get("updatedBy"),
    )
    return ValidationResult(accepted=True, rule=validated)


def default_rule_templates(property_id: str) -> list[RuleDefinition]:
    """Return the default rule templates seeded for a property at launch.

    Provides one enabled template per MVP alert-producing type (Requirement
    2.2: each template's ``enabled`` flag is true by default). CRITICAL and
    WARNING templates enable agent triage; INFO templates do not.

    Note:
        Requirement 2.2 references "eight MVP alert types" (UC-01 through
        UC-08). UC-07 (escalation routing) and UC-08 (history / shift handover)
        are platform capabilities, not rule-driven alert-producing types, so
        they have no rule template. The six alert-producing types (UC-01
        through UC-06) each receive a template here. See the module follow-up
        note for the naming discrepancy.

    Args:
        property_id: The property to seed templates for.

    Returns:
        The default, enabled rule definitions for the property, one per
        alert-producing type.
    """
    templates: list[RuleDefinition] = []
    for alert_type, tier in _DEFAULT_TIER_BY_TYPE.items():
        templates.append(
            RuleDefinition(
                property_id=property_id,
                rule_type=alert_type,
                tier=tier,
                trigger_condition=dict(_DEFAULT_TRIGGER_BY_TYPE[alert_type]),
                # All six seeded types request a triage brief: CRITICAL/WARNING
                # get an action brief and the two INFO types (premium
                # cancellation, VIP check-in) get an advisory opportunity brief.
                agent_triage_enabled=True,
                escalation_timeout_sec=DEFAULT_ESCALATION_TIMEOUT_SEC,
                enabled=True,
                parameters=dict(_DEFAULT_PARAMETERS_BY_TYPE[alert_type]),
            )
        )
    return templates


def default_template_item(rule: RuleDefinition) -> dict[str, Any]:
    """Serialize a rule definition into a ``pulse-rules`` DynamoDB item.

    Attribute keys are camelCase (NAMING-05), keyed by ``propertyId`` +
    ``ruleType`` (design "pulse-rules" model).

    Args:
        rule: The rule definition to serialize.

    Returns:
        A DynamoDB item dict (native Python types) ready for ``put_item``.
    """
    return {
        "propertyId": rule.property_id,
        "ruleType": rule.rule_type.value,
        "tier": rule.tier.value,
        "triggerCondition": dict(rule.trigger_condition),
        "parameters": dict(rule.parameters),
        "agentTriageEnabled": rule.agent_triage_enabled,
        "escalationTimeoutSec": rule.escalation_timeout_sec,
        "enabled": rule.enabled,
        "updatedAt": rule.updated_at,
        "updatedBy": rule.updated_by,
    }


__all__ = [
    "ESCALATION_TIMEOUT_MIN_SEC",
    "ESCALATION_TIMEOUT_MAX_SEC",
    "REQUIRED_ATTRIBUTES",
    "DEFAULT_ESCALATION_TIMEOUT_SEC",
    "ValidationResult",
    "validate_rule_update",
    "default_rule_templates",
    "default_template_item",
]
