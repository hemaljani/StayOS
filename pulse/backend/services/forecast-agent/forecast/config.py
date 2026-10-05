"""Runtime configuration for the PULSE Forecasting Agent AgentCore service.

Reads the environment variables the predictive forecasting runtime needs,
injected by AgentCore Runtime at deploy time from CloudFormation outputs and SSM
Parameter Store (never hardcoded; PYQUALITY-06 / NAMING-02, -03). This mirrors
the sibling Triage Agent's ``config.py`` conventions: a frozen dataclass built by
a single ``load_*`` function, env-var names declared once as module constants,
and a fail-fast helper for required values. The forecasting service reads only
what the read -> forecast -> author -> reconcile flow plus the realtime publish
need, so it deliberately does NOT reuse ``pulse.common.config.load_config``
(which pulls in unrelated pipeline table identifiers).

Environment variables:
    FORECAST_ENABLED_PROPERTY_IDS: Comma-delimited allowlist of property ids the
        forecaster is permitted to run for. Parsed into a ``frozenset[str]``
        (whitespace stripped, empties dropped). The invocation gate rejects any
        ``propertyId`` not in this set.
    FORECAST_HORIZON_DAYS: Number of days ahead the Forecast Engine evaluates.
        Defaults to 14 and is clamped to the inclusive range 1..14 (Req 3.1).
    FORECAST_CONFIDENCE_THRESHOLD: Integer 0-100 minimum confidence at or above
        which an advisory alert is written. Defaults to 50.
    FORECAST_MIN_DATA_POINTS: Minimum number of forward data points required
        before a prediction is trusted. Defaults to 3.
    ALERTS_TABLE_NAME: Physical name of the ``pulse-alerts`` table (the
        conditional predictive-alert write target).
    GATEWAY_ENDPOINT_URL: Shared StayOS AgentCore Gateway MCP endpoint
        (Streamable HTTP, IAM auth). Sourced from SSM
        ``/${StackPrefix}/gateway/endpoint-url``.
    REALTIME_HTTP_ENDPOINT: AppSync Events HTTP publish endpoint. Optional; when
        unset the realtime publish is skipped (best-effort).
    FORECAST_MODEL_ID: Bedrock model id the Remediation Plan Author uses to
        author (never recompute) the plan narrative. Defaults to the LUMI chat
        model id.
    POWERTOOLS_SERVICE_NAME: Service dimension for structured logs / EMF metrics.
        Defaults to ``pulse-forecaster``.
    LOG_LEVEL: Log level for the module-level logger. Defaults to ``INFO``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from aws_lambda_powertools import Logger

# Environment variable names (single source of truth for this service).
ENV_ENABLED_PROPERTY_IDS = "FORECAST_ENABLED_PROPERTY_IDS"
ENV_HORIZON_DAYS = "FORECAST_HORIZON_DAYS"
ENV_CONFIDENCE_THRESHOLD = "FORECAST_CONFIDENCE_THRESHOLD"
ENV_MIN_DATA_POINTS = "FORECAST_MIN_DATA_POINTS"
ENV_ALERTS_TABLE_NAME = "ALERTS_TABLE_NAME"
ENV_GATEWAY_ENDPOINT_URL = "GATEWAY_ENDPOINT_URL"
ENV_REALTIME_HTTP_ENDPOINT = "REALTIME_HTTP_ENDPOINT"
ENV_MODEL_ID = "FORECAST_MODEL_ID"
ENV_SERVICE_NAME = "POWERTOOLS_SERVICE_NAME"
ENV_LOG_LEVEL = "LOG_LEVEL"

# Defaults for the optional / bounded values (spec: task 4.1). Kept as
# module-level UPPER_SNAKE constants so the bounds and fallbacks are visible in
# one place (PYQUALITY-05 / NAMING-03).
DEFAULT_HORIZON_DAYS = 14
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 14
DEFAULT_CONFIDENCE_THRESHOLD = 50
DEFAULT_MIN_DATA_POINTS = 3
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_SERVICE_NAME = "pulse-forecaster"
DEFAULT_LOG_LEVEL = "INFO"

# The delimiter used in FORECAST_ENABLED_PROPERTY_IDS.
PROPERTY_ID_DELIMITER = ","

logger = Logger(service=DEFAULT_SERVICE_NAME)


class ConfigError(Exception):
    """Raised when a required configuration value is missing or malformed.

    Kept local to this service so the forecaster fails fast at cold start with a
    clear, service-specific message rather than surfacing a generic
    ``KeyError``/``ValueError`` mid-invocation.
    """


def _get(env: Mapping[str, str], name: str) -> str:
    """Read and strip an environment variable, returning ``""`` when unset.

    Args:
        env: The environment mapping to read from.
        name: The environment variable name.

    Returns:
        The stripped value, or an empty string when the variable is unset.
    """
    return env.get(name, "").strip()


def _require(env: Mapping[str, str], name: str) -> str:
    """Read a required environment variable or fail fast.

    Args:
        env: The environment mapping to read from.
        name: The environment variable name to read.

    Returns:
        The non-empty, stripped environment variable value.

    Raises:
        ConfigError: If the variable is unset or empty (a deploy bug).
    """
    value = _get(env, name)
    if not value:
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse an integer environment variable, falling back to a default.

    An unset (or blank) variable yields ``default``. A present-but-non-integer
    value is a deploy misconfiguration and raises rather than silently defaulting.

    Args:
        env: The environment mapping to read from.
        name: The environment variable name.
        default: The value to use when the variable is unset or blank.

    Returns:
        The parsed integer, or ``default`` when unset/blank.

    Raises:
        ConfigError: If the variable is set to a non-integer value.
    """
    raw = _get(env, name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable {name!r} must be an integer, got {raw!r}"
        ) from exc


def _parse_property_ids(env: Mapping[str, str], name: str) -> frozenset[str]:
    """Parse a comma-delimited property-id allowlist into a frozenset.

    Whitespace around each id is stripped and empty segments (e.g. from a
    trailing comma) are dropped, so ``"p1, p2,"`` yields ``{"p1", "p2"}``.

    Args:
        env: The environment mapping to read from.
        name: The environment variable name.

    Returns:
        The set of enabled property ids (empty when the variable is unset).
    """
    raw = _get(env, name)
    if not raw:
        return frozenset()
    return frozenset(
        segment.strip()
        for segment in raw.split(PROPERTY_ID_DELIMITER)
        if segment.strip()
    )


@dataclass(frozen=True)
class ForecastConfig:
    """Immutable, typed view of the forecasting runtime's configuration.

    Attributes:
        enabled_property_ids: Allowlist of property ids the forecaster may run
            for; empty when unset.
        horizon_days: Days ahead the engine evaluates, clamped to 1..14.
        confidence_threshold: Minimum confidence (0-100) to write an alert.
        min_data_points: Minimum forward data points required to trust a
            prediction.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        gateway_endpoint_url: Shared StayOS Gateway MCP endpoint URL.
        realtime_http_endpoint: AppSync Events publish endpoint, or ``None`` when
            realtime publishing is disabled.
        model_id: Bedrock model id used by the Remediation Plan Author.
        service_name: Powertools service dimension for logs/metrics.
        log_level: Log level for the module-level logger.
    """

    enabled_property_ids: frozenset[str]
    horizon_days: int
    confidence_threshold: int
    min_data_points: int
    alerts_table_name: str
    gateway_endpoint_url: str
    realtime_http_endpoint: Optional[str]
    model_id: str
    service_name: str
    log_level: str


def load_config(env: Optional[Mapping[str, str]] = None) -> ForecastConfig:
    """Load and validate the forecasting runtime configuration.

    Reads from ``os.environ`` by default; an explicit mapping may be passed in for
    testability (so callers can exercise parsing without mutating the process
    environment).

    Args:
        env: Optional environment mapping override. Defaults to ``os.environ``.

    Returns:
        A validated, immutable :class:`ForecastConfig`.

    Raises:
        ConfigError: If a required variable (``ALERTS_TABLE_NAME``,
            ``GATEWAY_ENDPOINT_URL``) is missing, or a bounded/int variable is
            malformed.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    # Clamp the horizon to the inclusive 1..14 window (Req 3.1): a value outside
    # the range is corrected to the nearest bound rather than rejected.
    requested_horizon = _parse_int(source, ENV_HORIZON_DAYS, DEFAULT_HORIZON_DAYS)
    horizon_days = max(MIN_HORIZON_DAYS, min(MAX_HORIZON_DAYS, requested_horizon))

    realtime_endpoint = _get(source, ENV_REALTIME_HTTP_ENDPOINT)

    config = ForecastConfig(
        enabled_property_ids=_parse_property_ids(source, ENV_ENABLED_PROPERTY_IDS),
        horizon_days=horizon_days,
        confidence_threshold=_parse_int(
            source, ENV_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
        ),
        min_data_points=_parse_int(
            source, ENV_MIN_DATA_POINTS, DEFAULT_MIN_DATA_POINTS
        ),
        alerts_table_name=_require(source, ENV_ALERTS_TABLE_NAME),
        gateway_endpoint_url=_require(source, ENV_GATEWAY_ENDPOINT_URL),
        # Optional: an unset endpoint disables realtime publishing.
        realtime_http_endpoint=realtime_endpoint or None,
        model_id=_get(source, ENV_MODEL_ID) or DEFAULT_MODEL_ID,
        service_name=_get(source, ENV_SERVICE_NAME) or DEFAULT_SERVICE_NAME,
        log_level=(_get(source, ENV_LOG_LEVEL) or DEFAULT_LOG_LEVEL).upper(),
    )

    if requested_horizon != horizon_days:
        logger.info(
            "Clamped forecast horizon to allowed range",
            requested_horizon=requested_horizon,
            effective_horizon=horizon_days,
        )
    logger.setLevel(config.log_level)
    return config


__all__ = [
    "ENV_ENABLED_PROPERTY_IDS",
    "ENV_HORIZON_DAYS",
    "ENV_CONFIDENCE_THRESHOLD",
    "ENV_MIN_DATA_POINTS",
    "ENV_ALERTS_TABLE_NAME",
    "ENV_GATEWAY_ENDPOINT_URL",
    "ENV_REALTIME_HTTP_ENDPOINT",
    "ENV_MODEL_ID",
    "ENV_SERVICE_NAME",
    "ENV_LOG_LEVEL",
    "ConfigError",
    "ForecastConfig",
    "load_config",
]
