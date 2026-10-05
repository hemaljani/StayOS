"""Rule-administration route logic: ``PUT /rules/{ruleId}`` (Requirement 2.4/2.5).

Property administrators edit alert rule definitions without a code deployment.
This module validates a submitted update via
:func:`pulse.rule_engine.rule_validation.validate_rule_update` and, only when the
update is accepted, persists it to ``pulse-rules``. On rejection nothing is
written, so the previously stored definition is retained unchanged and the
caller is told which attribute failed (Requirement 2.5).

A rule is keyed by ``propertyId`` + ``ruleType`` (design "pulse-rules" model).
The ``{ruleId}`` path parameter is the composite ``"{propertyId}#{ruleType}"``;
its components are merged into the submitted body so the identity in the URL and
the body cannot disagree. Every update is additionally scoped to the caller's
associated properties (Requirement 16.6).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Optional

from pulse.api.identity import CallerIdentity
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.rule_engine.rule_validation import (
    ValidationResult,
    default_template_item,
    validate_rule_update,
)

logger = get_logger("pulse-api")

# Separator between the two key components inside a composite ``{ruleId}``.
RULE_ID_SEPARATOR = "#"


def parse_rule_id(rule_id: str) -> tuple[Optional[str], Optional[str]]:
    """Split a composite ``{ruleId}`` into ``(propertyId, ruleType)``.

    Args:
        rule_id: The composite id ``"{propertyId}#{ruleType}"``.

    Returns:
        The ``(propertyId, ruleType)`` pair; either element is ``None`` when the
        id does not contain both components.
    """
    if RULE_ID_SEPARATOR not in rule_id:
        return None, None
    property_id, _, rule_type = rule_id.partition(RULE_ID_SEPARATOR)
    return (property_id or None), (rule_type or None)


def _merge_identity_into_update(
    rule_id: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge the composite ``{ruleId}`` components into the submitted body.

    The path components take precedence so the URL identity is authoritative.

    Args:
        rule_id: The composite ``{ruleId}`` from the path.
        body: The submitted update body.

    Returns:
        The update with ``propertyId``/``ruleType`` set from the path when the
        id is composite.
    """
    update = dict(body)
    property_id, rule_type = parse_rule_id(rule_id)
    if property_id is not None:
        update["propertyId"] = property_id
    if rule_type is not None:
        update["ruleType"] = rule_type
    return update


def update_rule(
    rule_id: str,
    body: Mapping[str, Any],
    identity: CallerIdentity,
    *,
    rules_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
) -> dict[str, Any]:
    """Validate and persist a rule-definition update (Requirement 2.4/2.5).

    Validates the merged update; on acceptance writes the serialized definition
    to ``pulse-rules`` and returns it. On rejection nothing is written (the prior
    definition is retained) and the offending attribute is reported. The update
    is denied when the caller is not associated with the target property.

    Args:
        rule_id: The composite ``{ruleId}`` (``"{propertyId}#{ruleType}"``).
        body: The submitted rule-definition attributes (camelCase).
        identity: The authenticated caller (for property scoping).
        rules_table_name: The ``pulse-rules`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        A result dict with ``accepted`` and either the persisted ``rule`` item or
        the ``invalidAttribute`` / ``message`` describing the rejection, plus a
        ``denied`` flag when property scoping blocks the update.
    """
    update = _merge_identity_into_update(rule_id, body)

    target_property = update.get("propertyId")
    if not target_property or not identity.is_associated_with(str(target_property)):
        logger.warning(
            "Rule update denied by property scope",
            extra={"gmAlias": identity.gm_alias, "propertyId": target_property},
        )
        return {"accepted": False, "denied": True, "message": "property-not-associated"}

    result: ValidationResult = validate_rule_update(update)
    if not result.accepted or result.rule is None:
        logger.info(
            "Rule update rejected; prior definition retained",
            extra={
                "propertyId": target_property,
                "invalidAttribute": result.invalid_attribute,
            },
        )
        return {
            "accepted": False,
            "invalidAttribute": result.invalid_attribute,
            "message": result.message,
        }

    item = default_template_item(result.rule)
    table = table_getter(rules_table_name)
    # Persist the validated rule (upsert by propertyId + ruleType).
    table.put_item(Item=item)
    logger.info(
        "Rule update persisted",
        extra={"propertyId": target_property, "ruleType": item.get("ruleType")},
    )
    return {"accepted": True, "rule": item}


def list_rules(
    identity: CallerIdentity,
    *,
    requested_property: Optional[str],
    rules_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
) -> list[dict[str, Any]]:
    """List rule definitions for a property the caller is associated with.

    Args:
        identity: The authenticated caller.
        requested_property: The ``propertyId`` query filter.
        rules_table_name: The ``pulse-rules`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).

    Returns:
        The rule items for the property, or an empty list when the property is
        missing or out of scope (Requirement 16.6).
    """
    if not requested_property or not identity.is_associated_with(requested_property):
        return []
    from boto3.dynamodb.conditions import Key

    table = table_getter(rules_table_name)
    response = table.query(
        KeyConditionExpression=Key("propertyId").eq(requested_property)
    )
    return list(response.get("Items", []))


__all__ = [
    "RULE_ID_SEPARATOR",
    "parse_rule_id",
    "update_rule",
    "list_rules",
]
