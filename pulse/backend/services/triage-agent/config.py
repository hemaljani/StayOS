"""Runtime configuration for the PULSE Triage Agent AgentCore service.

Reads the handful of environment variables the request-response triage runtime
needs, injected by AgentCore Runtime at deploy time from CloudFormation outputs
and SSM Parameter Store (never hardcoded; PYQUALITY-06 / NAMING-02, -03). Unlike
the pipeline Lambdas, this service does not use the rules / history / push
tables, so it deliberately does NOT call ``pulse.common.config.load_config``
(which would require those unrelated identifiers). It reads only what triage
plus the conditional attach + realtime publish need.

Environment variables:
    GATEWAY_ENDPOINT_URL: Shared StayOS AgentCore Gateway MCP endpoint
        (Streamable HTTP, IAM auth). Sourced from SSM
        ``/${StackPrefix}/gateway/endpoint-url``.
    AWS_DEFAULT_REGION / AWS_REGION: Region for the Gateway SigV4 signer, the
        Bedrock model invocation, and boto3 clients.
    TRIAGE_MODEL_ID: Bedrock model id used for the narrative brief (matches
        LUMI's chat model). Read here for logging/visibility; the reused
        ``pulse.triage.bedrock_client`` resolves it again from the same env var.
    ALERTS_TABLE_NAME: Physical name of the ``pulse-alerts`` table (conditional
        triage-brief attach target).
    REALTIME_HTTP_ENDPOINT: AppSync Events HTTP publish endpoint. Read directly
        by ``pulse.delivery.realtime_publish``; documented here for completeness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pulse.common.errors import ConfigurationError

# Environment variable names (single source of truth for this service).
ENV_GATEWAY_ENDPOINT_URL = "GATEWAY_ENDPOINT_URL"
ENV_TRIAGE_MODEL_ID = "TRIAGE_MODEL_ID"
ENV_ALERTS_TABLE_NAME = "ALERTS_TABLE_NAME"
ENV_AWS_REGION = "AWS_DEFAULT_REGION"

# Gateway inbound auth uses the AgentCore service namespace for the SigV4 signer,
# exactly as the LUMI chat agent does (aws_service="bedrock-agentcore").
GATEWAY_AWS_SERVICE = "bedrock-agentcore"


def _require_env(name: str) -> str:
    """Read a required environment variable or fail fast.

    Args:
        name: The environment variable name to read.

    Returns:
        The non-empty environment variable value.

    Raises:
        ConfigurationError: If the variable is unset or empty (a deploy bug).
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"Required environment variable {name!r} is not set", variable=name
        )
    return value


@dataclass(frozen=True)
class TriageRuntimeConfig:
    """Immutable, typed view of the triage runtime's configuration.

    Attributes:
        gateway_endpoint_url: Shared StayOS Gateway MCP endpoint URL.
        region: AWS region for the Gateway signer, Bedrock, and boto3.
        triage_model_id: Bedrock model id for the narrative brief.
        alerts_table_name: The ``pulse-alerts`` physical table name.
    """

    gateway_endpoint_url: str
    region: str
    triage_model_id: str
    alerts_table_name: str


def load_runtime_config() -> TriageRuntimeConfig:
    """Load and validate the triage runtime configuration from the environment.

    Returns:
        A validated, immutable :class:`TriageRuntimeConfig`.

    Raises:
        ConfigurationError: If any required variable is missing (fail fast at
            cold start rather than mid-invocation).
    """
    return TriageRuntimeConfig(
        gateway_endpoint_url=_require_env(ENV_GATEWAY_ENDPOINT_URL),
        # AgentCore injects AWS_DEFAULT_REGION; default to us-east-1 to match the
        # LUMI chat agent when running locally.
        region=os.environ.get(ENV_AWS_REGION, "us-east-1").strip() or "us-east-1",
        triage_model_id=_require_env(ENV_TRIAGE_MODEL_ID),
        alerts_table_name=_require_env(ENV_ALERTS_TABLE_NAME),
    )


__all__ = [
    "ENV_GATEWAY_ENDPOINT_URL",
    "ENV_TRIAGE_MODEL_ID",
    "ENV_ALERTS_TABLE_NAME",
    "ENV_AWS_REGION",
    "GATEWAY_AWS_SERVICE",
    "TriageRuntimeConfig",
    "load_runtime_config",
]
