"""First-deploy application-data seed provisioning for the orchestrator.

The orchestrator's ``mode: "seed"`` path owns the FULL first-deploy seed
(Requirement 1.3): the LUMI operational dataset (rooms, guests, revenues,
reservations, work-orders) AND the LUMI application data (Cognito GM users,
per-GM settings, per-GM brief schedules, historical briefs) AND the PULSE seed
data (rules / kitchen baseline).

The dataset window is generated in-process by the Generate/Reconcile steps via
:mod:`generation_runner`. This module owns the *other* half - the application
and PULSE seed data - WITHOUT duplicating that logic. The existing LUMI
seed-data Lambda (``lumi/backend/functions/seed-data/lambda_function.py``)
already provisions Cognito users, settings, schedules, and historical briefs
idempotently; the orchestrator therefore *invokes* it in seed mode rather than
re-implementing it (design "Components and Interfaces"; golden-rule: move/reuse
rather than duplicate).

Because the LUMI seed Lambda is upsert-only unless a two-factor confirmation is
supplied, invoking it from the automated seed path is safe: the orchestrator
NEVER sends the ``Force``/``ConfirmClear`` confirmation, so the invoked seed is
always idempotent and never destructive (Requirements 8.1, 8.2).

Satisfies: Requirement 1.3 (full first-deploy seed), 8.1 (no destructive op on
the automated path).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

from orchestrator_common import SERVICE_NAME

logger = Logger(service=SERVICE_NAME)

# Environment variable carrying the ARN of the LUMI application-data seed
# Lambda (PYQUALITY-06 / NAMING); never hardcoded. When unset, application-data
# provisioning is skipped (graceful degradation) so the dataset seed still runs.
ENV_SEED_LAMBDA_ARN = "SEED_LAMBDA_ARN"

# Optional env var carrying the ARN of the PULSE seed Lambda (rules / kitchen
# baseline). When unset, PULSE seed provisioning is skipped gracefully.
ENV_PULSE_SEED_LAMBDA_ARN = "PULSE_SEED_LAMBDA_ARN"

# Module-level client with an explicit standard-mode retry config (PYQUALITY-06),
# created once per container for connection reuse across invocations.
_LAMBDA_CLIENT = boto3.client(
    "lambda",
    config=Config(retries={"mode": "standard", "max_attempts": 5}),
)


def _invoke_seed_lambda(function_arn: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronously invoke a seed Lambda and return a structured outcome.

    The seed Lambdas are CloudFormation custom-resource handlers that also
    accept direct invocation (they skip the CFN response when no
    ``ResponseURL`` is present). We invoke synchronously (``RequestResponse``)
    so the orchestrator step reports the real outcome in its summary
    (Requirement 9.2) rather than fire-and-forget.

    Args:
        function_arn: The ARN of the seed Lambda to invoke.
        payload: The invocation event. It intentionally carries NO ``Force`` or
            ``ConfirmClear`` field, so the invoked seed stays upsert-only
            (Requirements 8.1, 8.2).

    Returns:
        A structured result dict: ``{"invoked": True, "statusCode": int,
        "functionError": Optional[str]}``.

    Raises:
        _LAMBDA_CLIENT.exceptions.ResourceNotFoundException: If the ARN is
            unknown.
    """
    response = _LAMBDA_CLIENT.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    # A non-empty FunctionError means the invoked handler raised; surface it so
    # the orchestrator does not mask a failed seed as success (Requirement 9.3).
    function_error: Optional[str] = response.get("FunctionError")
    return {
        "invoked": True,
        "statusCode": response.get("StatusCode"),
        "functionError": function_error,
    }


def provision_application_seed(request_type: str = "Create") -> Dict[str, Any]:
    """Provision the LUMI + PULSE application/first-deploy seed data.

    Invokes the existing LUMI seed Lambda (Cognito users, settings, schedules,
    historical briefs, dataset) and, when configured, the PULSE seed Lambda
    (rules / kitchen baseline). This reuses the existing, idempotent seed logic
    rather than duplicating it. Neither invocation carries a destructive-clear
    confirmation, so both stay upsert-only (Requirements 8.1, 8.2, 1.3).

    Both invocations degrade gracefully: a missing ARN is logged and skipped,
    and an invocation error is recorded in the returned detail without aborting
    the rest of the seed (mirrors the LUMI seed Lambda's own step-level graceful
    degradation).

    Args:
        request_type: The custom-resource-style request type to forward to the
            invoked seed Lambda (``Create`` for a first deploy). Defaults to
            ``Create``.

    Returns:
        Structured detail describing which seed Lambdas were invoked and their
        outcomes, suitable for the Generate step's result envelope.
    """
    detail: Dict[str, Any] = {
        "lumiSeedInvoked": False,
        "pulseSeedInvoked": False,
    }

    # The payload deliberately omits Force/ConfirmClear so the invoked seed is
    # upsert-only and can never clear tables from the automated seed path.
    seed_payload: Dict[str, Any] = {"RequestType": request_type}

    lumi_seed_arn = os.environ.get(ENV_SEED_LAMBDA_ARN)
    if lumi_seed_arn:
        try:
            outcome = _invoke_seed_lambda(lumi_seed_arn, seed_payload)
            detail["lumiSeedInvoked"] = True
            detail["lumiSeed"] = outcome
            if outcome["functionError"]:
                logger.error(
                    "LUMI application seed reported a function error",
                    extra={"functionError": outcome["functionError"]},
                )
            else:
                logger.info("LUMI application seed invoked", extra=outcome)
        except _LAMBDA_CLIENT.exceptions.ResourceNotFoundException:
            # A stale/incorrect ARN should not abort the dataset seed.
            logger.error(
                "LUMI seed Lambda ARN not found - skipping application seed",
                extra={"seedLambdaArn": lumi_seed_arn},
            )
            detail["lumiSeed"] = {"invoked": False, "error": "ResourceNotFound"}
    else:
        logger.warning(
            "SEED_LAMBDA_ARN not configured - skipping LUMI application seed "
            "(dataset window still generated by the Generate step)"
        )

    pulse_seed_arn = os.environ.get(ENV_PULSE_SEED_LAMBDA_ARN)
    if pulse_seed_arn:
        try:
            outcome = _invoke_seed_lambda(pulse_seed_arn, seed_payload)
            detail["pulseSeedInvoked"] = True
            detail["pulseSeed"] = outcome
            if outcome["functionError"]:
                logger.error(
                    "PULSE seed reported a function error",
                    extra={"functionError": outcome["functionError"]},
                )
            else:
                logger.info("PULSE seed invoked", extra=outcome)
        except _LAMBDA_CLIENT.exceptions.ResourceNotFoundException:
            logger.error(
                "PULSE seed Lambda ARN not found - skipping PULSE seed",
                extra={"pulseSeedLambdaArn": pulse_seed_arn},
            )
            detail["pulseSeed"] = {"invoked": False, "error": "ResourceNotFound"}
    else:
        logger.info(
            "PULSE_SEED_LAMBDA_ARN not configured - skipping PULSE seed provisioning"
        )

    return detail
