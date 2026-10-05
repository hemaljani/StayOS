"""Rule Engine Lambda entry point (``pulse-rule-evaluator``).

This module is a thin orchestrator (PYQUALITY-05): :func:`lambda_handler` parses
a DynamoDB Streams batch into :class:`OperationalChange` objects and delegates
all decision-making to pure functions -- :func:`evaluate_rules` (which rules
fire) and the per-type evaluators in :mod:`pulse.rule_engine.evaluators`. The
handler then persists each resulting draft with a dedupe-guarded conditional
write (at most one alert per condition, Property 16), and -- for CRITICAL or
WARNING alerts whose rule enables triage -- fires the Triage Agent runtime
asynchronously once the alert is delivered.

Error isolation follows the design's "never drop the batch" principle:
    * A single un-processable stream record logs an evaluation error and the
      handler continues with the next record (Requirement 1.6).
    * A single rule that cannot be evaluated (missing source data) logs an
      evaluation error and the remaining rules still run (Requirement 1.6).
    * Triage is invoked asynchronously and best-effort after delivery (design
      Decision 8): a missing runtime ARN or an invocation error only logs a
      warning and never fails alert creation or delivery (Requirements 1.4,
      1.7). The runtime attaches the brief and publishes ``ALERT_UPDATED``
      itself, so the evaluator never attaches a brief.

Importing this module also imports the evaluators module, whose import
registers every per-type evaluator into the shared registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeDeserializer

from pulse.common.config import load_config
from pulse.common.dynamo import get_table
from pulse.common.errors import RuleEvaluationError
from pulse.common.logging import get_logger
from pulse.common.models import (
    AlertDraft,
    AlertTier,
    OperationalChange,
    RuleDefinition,
)
from pulse.common.tracing import get_tracer

# Importing evaluators has the side effect of registering all per-type
# evaluators into the registry used by evaluate_rules (see rules_repository).
from pulse.rule_engine import evaluators  # noqa: F401
from pulse.rule_engine.alert_factory import draft_to_item
from pulse.rule_engine.loop_guard import resolve_cleared_correlations
from pulse.rule_engine.rules_repository import (
    RulesRepository,
    get_default_repository,
    get_evaluator,
)
from pulse.rule_engine.triage_invoker import invoke_triage_async

logger = get_logger("pulse-rule-evaluator")
tracer = get_tracer("pulse-rule-evaluator")

_deserializer = TypeDeserializer()

# Tiers that are eligible for agent triage. CRITICAL/WARNING alerts get an
# action brief; the two INFO types (premium cancellation, VIP check-in) now get
# an advisory "opportunity" brief. Whether an individual rule is triaged is still
# gated by its ``agentTriageEnabled`` flag (see should_request_triage).
_TRIAGE_ELIGIBLE_TIERS = frozenset(
    {AlertTier.CRITICAL, AlertTier.WARNING, AlertTier.INFO}
)


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------


def _table_from_arn(event_source_arn: Optional[str]) -> str:
    """Extract the source table name from a DynamoDB stream ARN.

    A stream ARN looks like
    ``arn:aws:dynamodb:<region>:<acct>:table/<name>/stream/<ts>``.

    Args:
        event_source_arn: The record's ``eventSourceARN`` (may be ``None``).

    Returns:
        The table name, or ``"unknown"`` when it cannot be parsed.
    """
    if not event_source_arn:
        return "unknown"
    parts = event_source_arn.split("/")
    # parts: [..., "table", "<name>", "stream", "<ts>"]
    if "table" in parts:
        idx = parts.index("table")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "unknown"


def _deserialize_image(
    raw_image: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Deserialize a DynamoDB stream image into native Python types.

    Args:
        raw_image: The raw ``AttributeValue`` image from the stream record, or
            ``None`` when absent.

    Returns:
        The deserialized image (native Python types), or ``None``.
    """
    if not raw_image:
        return None
    return {key: _deserializer.deserialize(value) for key, value in raw_image.items()}


def parse_stream_record(record: dict[str, Any]) -> OperationalChange:
    """Parse one raw DynamoDB Streams record into an :class:`OperationalChange`.

    Args:
        record: A single stream record from the Lambda event's ``Records`` list.

    Returns:
        The normalized operational change (native Python types, resolved
        property id where present).
    """
    dynamodb = record.get("dynamodb", {})
    new_image = _deserialize_image(dynamodb.get("NewImage"))
    old_image = _deserialize_image(dynamodb.get("OldImage"))
    # Prefer the new image for the property id, falling back to the old image
    # for REMOVE events.
    property_id: Optional[str] = None
    for image in (new_image, old_image):
        if image and image.get("propertyId"):
            property_id = image["propertyId"]
            break
    return OperationalChange(
        table=_table_from_arn(record.get("eventSourceARN")),
        event_name=record.get("eventName", "UNKNOWN"),
        property_id=property_id,
        new_image=new_image,
        old_image=old_image,
    )


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def evaluate_rules(
    change: OperationalChange, rules: list[RuleDefinition]
) -> list[AlertDraft]:
    """Evaluate an operational change against a set of enabled rules (pure).

    For each rule with a registered evaluator, the evaluator is invoked. A rule
    that cannot be evaluated because of missing source data logs an evaluation
    error and is skipped, so one bad rule never blocks the others (Requirement
    1.6). This function performs no I/O and is unit-testable without a Lambda
    context.

    Args:
        change: The normalized operational change.
        rules: The enabled rule definitions for the change's property. Callers
            pass only enabled rules, so disabled rules never produce an alert
            (Requirement 2.3).

    Returns:
        The alert drafts produced by all matched rules (possibly empty).
    """
    drafts: list[AlertDraft] = []
    for rule in rules:
        evaluator = get_evaluator(rule.rule_type)
        if evaluator is None:
            # No evaluator registered for this rule type; nothing to do.
            continue
        try:
            draft = evaluator(change, rule)
        except RuleEvaluationError as exc:
            # Requirement 1.6: record the evaluation error identifying the event
            # and rule, then continue with the next rule.
            logger.error(
                "Rule evaluation error; skipping rule and continuing",
                extra={
                    "ruleType": rule.rule_type.value,
                    "propertyId": rule.property_id,
                    "table": change.table,
                    "eventName": change.event_name,
                    "detail": exc.detail,
                    "error": exc.message,
                },
            )
            continue
        if draft is not None:
            drafts.append(draft)
    return drafts


def should_request_triage(tier: AlertTier, agent_triage_enabled: bool) -> bool:
    """Return whether an alert should be routed to the Triage Agent.

    Triage is requested for any eligible-tier alert (CRITICAL, WARNING, or INFO)
    whose rule has agent triage enabled. INFO alerts receive an advisory
    opportunity brief rather than an action brief.

    Args:
        tier: The alert tier.
        agent_triage_enabled: The rule's ``agentTriageEnabled`` flag.

    Returns:
        ``True`` if a triage brief should be requested.
    """
    return agent_triage_enabled and tier in _TRIAGE_ELIGIBLE_TIERS


# ---------------------------------------------------------------------------
# Persistence + triage routing (I/O)
# ---------------------------------------------------------------------------


def persist_alert_draft(draft: AlertDraft, table: Any) -> bool:
    """Persist an alert draft with a dedupe-guarded conditional write.

    The alert's partition key (``alertId``) is derived deterministically from
    the dedupe key, so a duplicate event for the same condition targets the same
    item; the ``attribute_not_exists(alertId)`` condition then blocks the second
    write. A conditional-check failure is the expected idempotent outcome and is
    swallowed (Property 16); any other error propagates.

    Args:
        draft: The alert draft to persist.
        table: The ``pulse-alerts`` DynamoDB table resource.

    Returns:
        ``True`` if a new alert item was written, ``False`` if an alert for the
        same condition already existed.
    """
    try:
        table.put_item(
            Item=draft_to_item(draft),
            ConditionExpression=Attr("alertId").not_exists(),
        )
        logger.info(
            "Alert created",
            extra={
                "alertId": draft.alert_id,
                "propertyId": draft.property_id,
                "tier": draft.tier.value,
                "type": draft.type.value,
            },
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # An alert for this exact condition already exists: idempotent no-op.
        logger.info(
            "Duplicate suppressed; alert already exists for condition",
            extra={"alertId": draft.alert_id, "dedupeKey": draft.dedupe_key},
        )
        return False


def route_for_triage(
    draft: AlertDraft,
    rule: RuleDefinition,
    invoker: Optional[Callable[[AlertDraft], None]] = None,
) -> None:
    """Fire the Triage Agent runtime for an eligible, already-delivered alert.

    No-op for rules with triage disabled. For any eligible-tier alert (CRITICAL,
    WARNING, or INFO) whose rule enables triage, the runtime is invoked
    asynchronously and best-effort via :func:`invoke_triage_async` (design
    Decision 8). INFO alerts receive an advisory opportunity brief. The
    evaluator does not attach a brief: the runtime attaches it and publishes
    ``ALERT_UPDATED`` itself. A missing runtime ARN or an invocation error is
    swallowed by the invoker and never fails delivery
    (Requirements 1.4, 1.7).

    Args:
        draft: The persisted, delivered alert draft.
        rule: The rule that produced the draft (for the triage-enabled flag).
        invoker: Async triage invoker to use; falls back to
            :func:`invoke_triage_async` when omitted. Injectable for testing.
    """
    if not should_request_triage(draft.tier, rule.agent_triage_enabled):
        return
    active_invoker = invoker if invoker is not None else invoke_triage_async
    active_invoker(draft)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def _process_change(
    change: OperationalChange,
    repository: RulesRepository,
    alerts_table: Any,
) -> int:
    """Evaluate one change, persist matched alerts, and route triage.

    Args:
        change: The normalized operational change.
        repository: The rules repository (cached rule loader).
        alerts_table: The ``pulse-alerts`` DynamoDB table resource.

    Returns:
        The number of new alerts created from this change.
    """
    if not change.property_id:
        logger.warning(
            "Skipping change with no resolvable propertyId",
            extra={"table": change.table, "eventName": change.event_name},
        )
        return 0

    rules = repository.get_enabled_rules(change.property_id)
    drafts = evaluate_rules(change, rules)
    rules_by_type = {rule.rule_type: rule for rule in rules}

    created = 0
    for draft in drafts:
        if persist_alert_draft(draft, alerts_table):
            created += 1
            # The alert is now persisted and delivered (via the pulse-alerts
            # stream fan-out); fire the Triage Agent runtime asynchronously.
            route_for_triage(draft, rules_by_type[draft.type])

    # Closed-loop safety net (design Decision 6): if this change cleared a
    # resolvable trigger condition from a non-executor source, resolve the
    # still-open correlated alert. This is a no-op when the executor already set
    # RESOLVED transactionally, and never creates a duplicate.
    resolve_cleared_correlations(change, rules, alerts_table)
    return created


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Rule Engine Lambda handler for a DynamoDB Streams batch.

    Parses each stream record, evaluates it against the affected property's
    enabled rules, persists matched alerts idempotently, and routes eligible
    alerts to triage. Each record is isolated: a failure processing one record
    logs an evaluation error and does not stop the batch (Requirement 1.6).

    Args:
        event: The Lambda event containing a ``Records`` list of DynamoDB stream
            records.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A summary dict with the number of records processed and alerts created.
    """
    config = load_config()
    alerts_table = get_table(config.alerts_table)
    repository = get_default_repository()

    records = event.get("Records", [])
    processed = 0
    created = 0
    for record in records:
        try:
            change = parse_stream_record(record)
            created += _process_change(change, repository, alerts_table)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - per-record isolation
            # Requirement 1.6: one un-processable record must not block the
            # batch. Log an evaluation error identifying the record and move on.
            logger.error(
                "Failed to process stream record; continuing batch",
                extra={
                    "eventID": record.get("eventID"),
                    "eventSourceARN": record.get("eventSourceARN"),
                    "error": str(exc),
                },
            )
    logger.info(
        "Stream batch processed",
        extra={"recordsProcessed": processed, "alertsCreated": created},
    )
    return {"recordsProcessed": processed, "alertsCreated": created}


__all__ = [
    "parse_stream_record",
    "evaluate_rules",
    "should_request_triage",
    "persist_alert_draft",
    "route_for_triage",
    "lambda_handler",
]
