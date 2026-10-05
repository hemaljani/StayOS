"""Alert draft construction and DynamoDB item serialization.

The Rule Engine's per-type evaluators build an :class:`AlertDraft` for each
matched rule via :func:`build_alert_draft`. The draft carries the structural
invariants required by Requirements 1.2 and 1.3: a unique ``alertId``, a
``propertyId``, an ``UNACKNOWLEDGED`` status (implied at persist time), a tier,
an alert type, a title of 1-200 characters, a detail of 1-2000 characters, an
ISO 8601 creation timestamp, a dedupe key, and a source-entity correlation
pointer.

Two design points are enforced here:

    * **Deterministic, dedupe-aware ``alertId``.** The identifier is derived
      from the ``dedupeKey`` with a UUIDv5, so two events that describe the
      *same* triggering condition produce the *same* ``alertId``. This makes the
      conditional write in the handler idempotent (at most one alert per
      condition, Property 16) while still yielding a distinct identifier for
      every distinct condition (uniqueness, Property 2).
    * **Length invariants.** Title and detail are clamped to their permitted
      maxima and must be non-empty, so a created alert always satisfies the
      1-200 / 1-2000 bounds (Property 2). An empty title or detail is a
      programming error in an evaluator and raises.

This module also serializes a draft into the ``pulse-alerts`` item shape
(camelCase attributes, NAMING-05) that the handler persists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from pulse.common.errors import RuleEvaluationError
from pulse.common.models import (
    AlertDraft,
    AlertStatus,
    AlertTier,
    AlertType,
    SourceEntityRef,
)

# Length bounds from Requirement 1.3.
TITLE_MAX_LEN = 200
DETAIL_MAX_LEN = 2000

# Stable namespace for deriving deterministic alert identifiers from a dedupe
# key. Fixed so the same dedupe key always maps to the same UUID across
# processes and invocations.
_ALERT_ID_NAMESPACE = uuid.UUID("6f3d1e2a-4c5b-4a6d-9e7f-1a2b3c4d5e6f")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix.

    Returns:
        The current time in ISO 8601 format, e.g. ``"2026-08-17T14:30:00Z"``.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def derive_alert_id(dedupe_key: str) -> str:
    """Derive a deterministic ``alertId`` from a dedupe key.

    The same dedupe key always yields the same identifier, so duplicate events
    for one condition collide on the alerts table partition key and the
    conditional write suppresses the duplicate (Property 16). Distinct
    conditions yield distinct identifiers (Property 2).

    Args:
        dedupe_key: The condition-identifying dedupe key.

    Returns:
        A stable ``alert-<hex>`` identifier for the dedupe key.
    """
    return f"alert-{uuid.uuid5(_ALERT_ID_NAMESPACE, dedupe_key).hex}"


def _clamp_required_text(value: str, max_len: int, field_name: str) -> str:
    """Validate a required text field is non-empty and clamp it to a maximum.

    Args:
        value: The raw text produced by an evaluator.
        max_len: The maximum permitted length.
        field_name: The field name, used in the error message.

    Returns:
        The text, truncated to ``max_len`` characters.

    Raises:
        RuleEvaluationError: If ``value`` is empty (an evaluator bug), since an
            alert must carry a 1-character-minimum title and detail.
    """
    if not value:
        raise RuleEvaluationError(
            f"Alert {field_name} must be non-empty", detail=field_name
        )
    return value[:max_len]


def build_alert_draft(
    *,
    property_id: str,
    tier: AlertTier,
    alert_type: AlertType,
    title: str,
    detail: str,
    dedupe_key: str,
    source_entity_ref: SourceEntityRef,
    created_at: Optional[str] = None,
    gm_alias: Optional[str] = None,
    incomplete_input_data: bool = False,
) -> AlertDraft:
    """Build a validated :class:`AlertDraft` for a matched rule.

    Args:
        property_id: The property the alert belongs to.
        tier: The alert tier (from the rule definition).
        alert_type: The alert type (from the rule definition).
        title: Human-readable title; clamped to 200 characters, must be
            non-empty.
        detail: Human-readable detail; clamped to 2000 characters, must be
            non-empty.
        dedupe_key: The condition-identifying dedupe key; also the seed for the
            deterministic ``alertId``.
        source_entity_ref: Correlation pointer back to the triggering record.
        created_at: ISO 8601 creation timestamp; generated when omitted.
        gm_alias: Owning GM alias, when known at draft time.
        incomplete_input_data: Whether the alert was built from partial inputs
            (Requirement 4.4).

    Returns:
        A fully-populated :class:`AlertDraft` satisfying the structural
        invariants of Requirements 1.2 and 1.3.

    Raises:
        RuleEvaluationError: If the title or detail is empty.
    """
    return AlertDraft(
        alert_id=derive_alert_id(dedupe_key),
        property_id=property_id,
        tier=tier,
        type=alert_type,
        title=_clamp_required_text(title, TITLE_MAX_LEN, "title"),
        detail=_clamp_required_text(detail, DETAIL_MAX_LEN, "detail"),
        created_at=created_at or utc_now_iso(),
        dedupe_key=dedupe_key,
        source_entity_ref=source_entity_ref,
        gm_alias=gm_alias,
        incomplete_input_data=incomplete_input_data,
    )


def draft_to_item(draft: AlertDraft) -> dict[str, Any]:
    """Serialize an :class:`AlertDraft` into a ``pulse-alerts`` DynamoDB item.

    Produces the initial alert item with ``status = UNACKNOWLEDGED`` and the
    default approval/escalation state (design Data Models). Attribute keys are
    camelCase (NAMING-05). The heavier ``triageBrief`` is attached later by the
    handler only when triage succeeds.

    Args:
        draft: The alert draft to serialize.

    Returns:
        A DynamoDB item dict (native Python types) ready for ``put_item``.
    """
    item: dict[str, Any] = {
        "alertId": draft.alert_id,
        "propertyId": draft.property_id,
        "tier": draft.tier.value,
        "type": draft.type.value,
        "title": draft.title,
        "detail": draft.detail,
        "status": AlertStatus.UNACKNOWLEDGED.value,
        "dedupeKey": draft.dedupe_key,
        "sourceEntityRef": dict(draft.source_entity_ref),
        "incompleteInputData": draft.incomplete_input_data,
        # Default lifecycle / gate state for a freshly created alert.
        "escalationStatus": "NONE",
        "escalationReasons": [],
        "approval": {
            "state": "PENDING",
            "selectedOption": None,
            "decidedBy": None,
            "decidedAt": None,
        },
        "acknowledgedBy": None,
        "acknowledgedAt": None,
        "resolvedBy": None,
        "resolvedAt": None,
        "createdAt": draft.created_at,
        "lastStatusChangeAt": draft.created_at,
    }
    # gmAlias is the HASH key of the gmAlias-status-index GSI. A GSI key
    # attribute must be a valid string or ABSENT - writing an explicit NULL
    # makes DynamoDB reject the whole PutItem ("Type mismatch for Index Key
    # gmAlias Expected: S Actual: NULL"). Property-level alerts (e.g. WALK_RISK,
    # OOO_CLUSTER) have no owning GM, so only include gmAlias when known; a
    # property-level alert then simply does not appear on the per-GM GSI, which
    # is correct. (See BUG-018.)
    if draft.gm_alias:
        item["gmAlias"] = draft.gm_alias
    return item


__all__ = [
    "TITLE_MAX_LEN",
    "DETAIL_MAX_LEN",
    "utc_now_iso",
    "derive_alert_id",
    "build_alert_draft",
    "draft_to_item",
]
