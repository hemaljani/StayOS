"""Environment-variable configuration loader for PULSE.

Per PYQUALITY-06 and NAMING-03, resource identifiers (table names, the Bedrock
model id, secret paths) are never hardcoded; they are injected by CloudFormation
as environment variables and read here. This module centralizes that reading so
components share one typed ``PulseConfig`` view instead of scattering
``os.environ`` lookups.

Two categories of value are distinguished:

    * Required resource identifiers (table names, model id). Absence is a
      deploy/config bug, so ``load_config`` raises ``ConfigurationError`` and the
      Lambda fails fast at cold start.
    * Tunable business thresholds with safe, documented defaults (confidence
      threshold, INFO batch interval, escalation timeout). These may be omitted
      and fall back to the defaults defined here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from pulse.common.errors import ConfigurationError

# ---------------------------------------------------------------------------
# Environment variable names (single source of truth).
# ---------------------------------------------------------------------------

ENV_ALERTS_TABLE = "ALERTS_TABLE_NAME"
ENV_RULES_TABLE = "RULES_TABLE_NAME"
ENV_ALERT_HISTORY_TABLE = "ALERT_HISTORY_TABLE_NAME"
ENV_PUSH_SUBSCRIPTIONS_TABLE = "PUSH_SUBSCRIPTIONS_TABLE_NAME"
ENV_KITCHEN_TABLE = "KITCHEN_TABLE_NAME"
ENV_TRIAGE_MODEL_ID = "TRIAGE_MODEL_ID"
ENV_AWS_REGION = "AWS_REGION"
ENV_CONFIDENCE_THRESHOLD = "TRIAGE_CONFIDENCE_THRESHOLD"
ENV_INFO_BATCH_INTERVAL_MIN = "INFO_BATCH_INTERVAL_MIN"
ENV_ESCALATION_TIMEOUT_MIN = "ESCALATION_TIMEOUT_MIN"

# ---------------------------------------------------------------------------
# Default values for tunable business thresholds (not resource identifiers).
# These mirror the defaults documented in requirements.md / design.md.
# ---------------------------------------------------------------------------

# Confidence below this percentage flags an alert for mandatory GM review
# (Requirement 10.4 / 11.4).
DEFAULT_CONFIDENCE_THRESHOLD = 85

# INFO alerts are batched on this interval in minutes (Requirement 13.3,
# default 15, valid range 5-60).
DEFAULT_INFO_BATCH_INTERVAL_MIN = 15

# Escalation timeout in minutes when a rule does not configure one
# (Requirement 6.4, default 5, accepted range 1-60).
DEFAULT_ESCALATION_TIMEOUT_MIN = 5


def _get_required_env(name: str) -> str:
    """Read a required environment variable or fail fast.

    Args:
        name: The environment variable name to read.

    Returns:
        The non-empty environment variable value.

    Raises:
        ConfigurationError: If the variable is unset or empty.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"Required environment variable {name!r} is not set", variable=name
        )
    return value


def _get_int_env(name: str, default: int) -> int:
    """Read an integer environment variable with a fallback default.

    Args:
        name: The environment variable name to read.
        default: The value to return when the variable is unset or empty.

    Returns:
        The parsed integer value, or ``default`` when unset/empty.

    Raises:
        ConfigurationError: If the variable is set but not a valid integer.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"Environment variable {name!r} must be an integer, got {raw!r}",
            variable=name,
        ) from exc


@dataclass(frozen=True)
class PulseConfig:
    """Immutable, typed view of PULSE runtime configuration.

    Load once at module level in each component via ``load_config()`` so a
    single, validated configuration object is reused across warm invocations.

    Attributes:
        alerts_table: Name of the ``pulse-alerts`` table.
        rules_table: Name of the ``pulse-rules`` table.
        alert_history_table: Name of the ``pulse-alert-history`` table.
        push_subscriptions_table: Name of the ``pulse-push-subscriptions`` table.
        kitchen_table: Name of the ``pulse-kitchen`` table, or ``None`` when not
            injected (only the API kitchen route needs it; pipeline Lambdas do
            not, so it is optional in the shared loader — see BUG-015).
        triage_model_id: Bedrock model id used by the Triage Agent.
        region: AWS region the component runs in.
        confidence_threshold: Confidence percentage below which an alert is
            flagged for mandatory GM review.
        info_batch_interval_min: INFO alert batching interval in minutes.
        escalation_timeout_min: Default escalation timeout in minutes.
    """

    alerts_table: str
    rules_table: str
    alert_history_table: str
    push_subscriptions_table: str
    kitchen_table: Optional[str]
    triage_model_id: str
    region: str
    confidence_threshold: int
    info_batch_interval_min: int
    escalation_timeout_min: int


def load_config() -> PulseConfig:
    """Load and validate PULSE configuration from the environment.

    Required resource identifiers must be present or a ``ConfigurationError`` is
    raised (fail fast at cold start). Tunable thresholds fall back to the
    module-level defaults when unset.

    Returns:
        A validated, immutable ``PulseConfig``.

    Raises:
        ConfigurationError: If a required variable is missing, or a numeric
            variable is present but not a valid integer.
    """
    return PulseConfig(
        alerts_table=_get_required_env(ENV_ALERTS_TABLE),
        rules_table=_get_required_env(ENV_RULES_TABLE),
        alert_history_table=_get_required_env(ENV_ALERT_HISTORY_TABLE),
        push_subscriptions_table=_get_required_env(ENV_PUSH_SUBSCRIPTIONS_TABLE),
        # Optional: only the API's kitchen route uses the kitchen table (via its
        # own required-env check). The stream/rule/escalation/delivery pipeline
        # Lambdas never touch it, so requiring it here would crash the entire
        # pipeline for a value it does not use (see BUG-015). Left optional so
        # the shared loader works in every component; the kitchen route still
        # enforces presence where it is actually needed.
        kitchen_table=get_optional_env(ENV_KITCHEN_TABLE),
        triage_model_id=_get_required_env(ENV_TRIAGE_MODEL_ID),
        # AWS_REGION is always injected by the Lambda runtime; treat it as
        # required so a non-Lambda misconfiguration is caught early.
        region=_get_required_env(ENV_AWS_REGION),
        confidence_threshold=_get_int_env(
            ENV_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
        ),
        info_batch_interval_min=_get_int_env(
            ENV_INFO_BATCH_INTERVAL_MIN, DEFAULT_INFO_BATCH_INTERVAL_MIN
        ),
        escalation_timeout_min=_get_int_env(
            ENV_ESCALATION_TIMEOUT_MIN, DEFAULT_ESCALATION_TIMEOUT_MIN
        ),
    )


def get_optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an optional string environment variable.

    A small convenience for components that need a single non-critical value
    (for example a secret ARN used only on one code path) without constructing
    the full ``PulseConfig``.

    Args:
        name: The environment variable name to read.
        default: The value to return when the variable is unset or empty.

    Returns:
        The environment variable value, or ``default`` when unset/empty.
    """
    value = os.environ.get(name, "").strip()
    return value or default


__all__ = [
    "PulseConfig",
    "load_config",
    "get_optional_env",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_INFO_BATCH_INTERVAL_MIN",
    "DEFAULT_ESCALATION_TIMEOUT_MIN",
    "ENV_ALERTS_TABLE",
    "ENV_RULES_TABLE",
    "ENV_ALERT_HISTORY_TABLE",
    "ENV_PUSH_SUBSCRIPTIONS_TABLE",
    "ENV_KITCHEN_TABLE",
    "ENV_TRIAGE_MODEL_ID",
    "ENV_AWS_REGION",
    "ENV_CONFIDENCE_THRESHOLD",
    "ENV_INFO_BATCH_INTERVAL_MIN",
    "ENV_ESCALATION_TIMEOUT_MIN",
]
