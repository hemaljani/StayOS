"""Rule loading, caching, and the declarative trigger-condition model.

This module owns three concerns for the Rule Engine (design Component 1):

    * **Rule loading + caching.** ``RulesRepository`` loads the *enabled* rule
      definitions for a property from the ``pulse-rules`` DynamoDB table and
      caches them in-process with a short TTL. Requirement 2.4 requires that a
      persisted rule update apply to evaluations that begin more than 60 seconds
      after persistence, so the cache TTL is bounded at 60 seconds by default:
      a rule change is guaranteed visible within that window without a
      per-event table read.
    * **Declarative trigger conditions (no ``eval``).** ``evaluate_condition``
      applies a small, fixed set of comparison operators to operands resolved
      from a caller-supplied context mapping. There is no code execution from
      the admin-editable table -- only named operators over named operands
      (design Component 1, "Trigger conditions are stored as a small, safe,
      declarative expression model").
    * **Evaluator registry.** Each ``AlertType`` maps to a registered evaluator
      function (registered from ``evaluators.py``). The Rule Engine looks the
      evaluator up by rule type; the stored ``triggerCondition`` supplies the
      thresholds/operands the evaluator needs.

All DynamoDB attribute keys read here are camelCase (NAMING-05); the Python
side is snake_case. Table names come from configuration, never hardcoded
(PYQUALITY-06).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Key

from pulse.common.config import load_config
from pulse.common.dynamo import get_table
from pulse.common.errors import RuleEvaluationError
from pulse.common.logging import get_logger
from pulse.common.models import (
    AlertDraft,
    AlertTier,
    AlertType,
    OperationalChange,
    RuleDefinition,
    TriggerCondition,
)

logger = get_logger("pulse-rule-evaluator")

# Maximum cache age. Bounded at 60s so a persisted rule update is visible to
# evaluations that begin more than 60s later (Requirement 2.4).
DEFAULT_CACHE_TTL_SECONDS = 60

# ---------------------------------------------------------------------------
# Evaluator registry
# ---------------------------------------------------------------------------

# An evaluator is a pure function: given a normalized operational change and a
# single (already-enabled) rule definition, it returns an AlertDraft when the
# rule's trigger fires, or None when it does not. It raises RuleEvaluationError
# when required source data is missing and the condition cannot be evaluated
# (Requirement 1.6).
EvaluatorFn = Callable[[OperationalChange, RuleDefinition], Optional[AlertDraft]]

_EVALUATOR_REGISTRY: dict[AlertType, EvaluatorFn] = {}


def register_evaluator(alert_type: AlertType) -> Callable[[EvaluatorFn], EvaluatorFn]:
    """Return a decorator that registers an evaluator for an alert type.

    Args:
        alert_type: The alert type (also the ``ruleType``) the decorated
            function evaluates.

    Returns:
        A decorator that records the function in the module-level registry and
        returns it unchanged.
    """

    def _decorator(func: EvaluatorFn) -> EvaluatorFn:
        _EVALUATOR_REGISTRY[alert_type] = func
        return func

    return _decorator


def get_evaluator(alert_type: AlertType) -> Optional[EvaluatorFn]:
    """Return the registered evaluator for an alert type, or ``None``.

    Args:
        alert_type: The alert type to look up.

    Returns:
        The registered evaluator function, or ``None`` when no evaluator is
        registered for the type.
    """
    return _EVALUATOR_REGISTRY.get(alert_type)


def registered_alert_types() -> frozenset[AlertType]:
    """Return the set of alert types that currently have an evaluator.

    Returns:
        An immutable set of registered alert types (useful for tests and
        introspection).
    """
    return frozenset(_EVALUATOR_REGISTRY)


# ---------------------------------------------------------------------------
# Declarative trigger-condition evaluation (no eval)
# ---------------------------------------------------------------------------

# Fixed operator table. Each entry is a two-argument comparison. Restricting to
# this table is what makes the admin-editable condition safe: an admin can only
# choose an operator by name, never inject executable code.
_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
}


def _resolve_operand(operand: Any, context: Mapping[str, Any]) -> Any:
    """Resolve a condition operand against the evaluation context.

    Resolution rules, in order:
        1. If ``operand`` is a string that names a key in ``context``, return
           the context value (this is how ``"reservations.confirmed"`` resolves
           to the live count the evaluator placed in the context).
        2. Otherwise the operand is treated as a literal and returned as-is
           (numbers, booleans, or string literals stored on the rule).

    Args:
        operand: The raw operand from the trigger condition (a context key name
            or a literal value).
        context: Mapping of operand-name to resolved value supplied by the
            evaluator.

    Returns:
        The resolved operand value.
    """
    if isinstance(operand, str) and operand in context:
        return context[operand]
    return operand


def evaluate_condition(condition: TriggerCondition, context: Mapping[str, Any]) -> bool:
    """Evaluate a declarative trigger condition against a context mapping.

    The condition names an ``operator`` and two operands (``left`` / ``right``).
    Operands are resolved via ``_resolve_operand`` (context key or literal) and
    compared using the fixed operator table. No expression string is ever
    executed.

    Args:
        condition: The declarative trigger condition (``operator``, ``left``,
            ``right``).
        context: Mapping of operand-name to resolved value.

    Returns:
        ``True`` if the comparison holds, ``False`` otherwise.

    Raises:
        RuleEvaluationError: If the operator is unknown, an operand is missing,
            or the two resolved operands cannot be compared.
    """
    operator = condition.get("operator")
    if operator not in _COMPARATORS:
        raise RuleEvaluationError(
            f"Unknown or missing trigger operator: {operator!r}",
            detail="operator",
        )
    if "left" not in condition or "right" not in condition:
        raise RuleEvaluationError(
            "Trigger condition is missing an operand", detail="operand"
        )

    left = _resolve_operand(condition["left"], context)
    right = _resolve_operand(condition["right"], context)
    try:
        return _COMPARATORS[operator](left, right)
    except TypeError as exc:
        # Incomparable operand types (e.g. a missing numeric resolved to None)
        # are treated as an un-evaluable condition, not a crash (Requirement
        # 1.6 handling happens in the caller).
        raise RuleEvaluationError(
            f"Operands are not comparable with {operator!r}: {left!r} vs {right!r}",
            detail="operand-type",
        ) from exc


# ---------------------------------------------------------------------------
# Rules repository (load + TTL cache)
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """A cached set of enabled rules for one property with a load timestamp.

    Attributes:
        rules: The enabled rule definitions for the property.
        loaded_monotonic: The monotonic clock value when the entry was loaded,
            used to compute cache age against the TTL.
    """

    rules: list[RuleDefinition]
    loaded_monotonic: float


def _to_int(value: Any) -> Optional[int]:
    """Coerce a DynamoDB numeric (``Decimal``) or ``int`` to ``int``.

    Args:
        value: The raw attribute value.

    Returns:
        The integer value, or ``None`` if ``value`` is ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return int(value)
    return int(str(value))


def item_to_rule_definition(item: Mapping[str, Any]) -> RuleDefinition:
    """Deserialize a ``pulse-rules`` DynamoDB item into a ``RuleDefinition``.

    Maps camelCase DynamoDB attributes to the snake_case dataclass fields and
    coerces enum-valued and numeric attributes to their Python types.

    Args:
        item: A DynamoDB item (native Python types, from the resource
            interface).

    Returns:
        The deserialized ``RuleDefinition``.

    Raises:
        RuleEvaluationError: If a required attribute is missing or an enum value
            is not recognized.
    """
    try:
        return RuleDefinition(
            property_id=item["propertyId"],
            rule_type=AlertType(item["ruleType"]),
            tier=AlertTier(item["tier"]),
            trigger_condition=dict(item.get("triggerCondition", {})),
            agent_triage_enabled=bool(item.get("agentTriageEnabled", False)),
            escalation_timeout_sec=_to_int(item.get("escalationTimeoutSec")) or 0,
            enabled=bool(item.get("enabled", False)),
            parameters=dict(item.get("parameters", {})),
            updated_at=item.get("updatedAt"),
            updated_by=item.get("updatedBy"),
        )
    except (KeyError, ValueError) as exc:
        raise RuleEvaluationError(
            f"Malformed rule item for property {item.get('propertyId')!r}: {exc}",
            detail="rule-deserialization",
        ) from exc


class RulesRepository:
    """Loads enabled rule definitions per property with a bounded-age cache.

    The repository queries the ``pulse-rules`` table by ``propertyId`` and
    caches the enabled subset in-process. Entries older than ``ttl_seconds`` are
    reloaded on next access, so a persisted rule update becomes visible within
    the TTL window (Requirement 2.4). A single repository instance is reused
    across warm Lambda invocations.

    Attributes:
        table_name: The ``pulse-rules`` physical table name.
        ttl_seconds: Maximum cache age before a reload, bounded at 60 seconds.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        table_getter: Callable[[str], Any] = get_table,
    ) -> None:
        """Initialize the repository.

        Args:
            table_name: The ``pulse-rules`` table name. When ``None`` it is read
                from configuration (``RULES_TABLE_NAME``) on first access.
            ttl_seconds: Cache TTL in seconds; clamped to at most 60 so
                Requirement 2.4's propagation guarantee always holds.
            clock: Monotonic clock function, injectable for tests.
            table_getter: Callable returning a DynamoDB ``Table`` for a name;
                injectable for tests to avoid a live table.
        """
        self._table_name = table_name
        self.ttl_seconds = min(ttl_seconds, DEFAULT_CACHE_TTL_SECONDS)
        self._clock = clock
        self._table_getter = table_getter
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def table_name(self) -> str:
        """Return the rules table name, loading it from config on first use.

        Returns:
            The ``pulse-rules`` physical table name.
        """
        if self._table_name is None:
            self._table_name = load_config().rules_table
        return self._table_name

    def _is_fresh(self, entry: _CacheEntry) -> bool:
        """Return whether a cache entry is still within the TTL window.

        Args:
            entry: The cache entry to check.

        Returns:
            ``True`` if the entry is younger than ``ttl_seconds``.
        """
        return (self._clock() - entry.loaded_monotonic) < self.ttl_seconds

    def _load_enabled_rules(self, property_id: str) -> list[RuleDefinition]:
        """Query the table for enabled rule definitions for a property.

        Args:
            property_id: The property whose rules to load.

        Returns:
            The enabled rule definitions for the property (Requirement 2.3
            excludes rules whose ``enabled`` flag is false).
        """
        table = self._table_getter(self.table_name)
        # Query by the propertyId partition key; a property has at most a
        # handful of rule types, so a single query (auto-paginated by the
        # resource interface for <=1MB) returns them all.
        response = table.query(KeyConditionExpression=Key("propertyId").eq(property_id))
        rules: list[RuleDefinition] = []
        for item in response.get("Items", []):
            rule = item_to_rule_definition(item)
            # Requirement 2.3: disabled rules never participate in evaluation.
            if rule.enabled:
                rules.append(rule)
        return rules

    def get_enabled_rules(self, property_id: str) -> list[RuleDefinition]:
        """Return the enabled rule definitions for a property (cached).

        On a cache miss or an expired entry the table is queried and the result
        cached; a fresh entry is served directly.

        Args:
            property_id: The property whose enabled rules to return.

        Returns:
            The enabled rule definitions for the property.
        """
        entry = self._cache.get(property_id)
        if entry is not None and self._is_fresh(entry):
            return entry.rules

        rules = self._load_enabled_rules(property_id)
        self._cache[property_id] = _CacheEntry(
            rules=rules, loaded_monotonic=self._clock()
        )
        return rules

    def invalidate(self, property_id: Optional[str] = None) -> None:
        """Evict cached rules for one property, or all properties.

        Args:
            property_id: The property to evict; when ``None`` the entire cache
                is cleared.
        """
        if property_id is None:
            self._cache.clear()
        else:
            self._cache.pop(property_id, None)


# A module-level repository reused across warm invocations. Constructed lazily
# so importing this module never requires configuration to be present (tests
# construct their own instances with an injected table name/clock).
_default_repository: Optional[RulesRepository] = None


def get_default_repository() -> RulesRepository:
    """Return the shared module-level ``RulesRepository`` instance.

    Returns:
        The lazily-constructed default repository (created on first call).
    """
    global _default_repository
    if _default_repository is None:
        _default_repository = RulesRepository()
    return _default_repository


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "EvaluatorFn",
    "register_evaluator",
    "get_evaluator",
    "registered_alert_types",
    "evaluate_condition",
    "item_to_rule_definition",
    "RulesRepository",
    "get_default_repository",
]

# NOTE: The per-type evaluators register themselves into ``_EVALUATOR_REGISTRY``
# as an import side effect (see ``evaluators.py``). The handler imports
# ``pulse.rule_engine.evaluators`` explicitly at module load so the registry is
# populated before any stream event is processed. Importing evaluators here
# would create a circular import (evaluators depends on this module), so it is
# intentionally left to the handler.
