"""LUMI Orchestrator Lambda - daily brief generation pipeline.

Main entry point for the stayos-orchestrator Lambda function. Triggered by
EventBridge Scheduler at each GM's configured delivery time (per-GM schedule)
or manually via generate-all action. Orchestrates the full pipeline: data pull,
action prioritization, AI narrative generation, validation, audio synthesis,
and DynamoDB storage.

Satisfies REQ-13 (Brief Generation Orchestration), REQ-SCHED-4 (Single-GM
Invocation Mode), with graceful degradation at each pipeline stage.
"""

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.metrics import EphemeralMetrics, MetricUnit
from botocore.config import Config
from botocore.exceptions import ClientError

from action_prioritizer import prioritize_actions
from audio_synthesizer import synthesize_audio
from brief_generator import generate_brief_narrative
from data_puller import pull_property_data
from data_validator import validate_narrative
from orchestrator_exceptions import (
    AllSourcesFailedError,
    AudioSynthesisError,
    BriefGenerationError,
)

logger = Logger(service="stayos-orchestrator")

# Module-level tracer for X-Ray distributed tracing (REQ-TEL-3)
tracer = Tracer(service="stayos-orchestrator")

# Module-level configuration from environment variables
BRIEFS_TABLE_NAME = os.environ.get("BRIEFS_TABLE_NAME", "stayos-briefs")
SETTINGS_TABLE_NAME = os.environ.get("SETTINGS_TABLE_NAME", "stayos-settings")

# TTL: 30 days in seconds
TTL_DAYS = 30
TTL_SECONDS = TTL_DAYS * 24 * 60 * 60

# Module-level boto3 resources and clients (connection reuse across invocations)
_dynamodb_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_dynamodb_resource = boto3.resource("dynamodb", config=_dynamodb_config)

# EphemeralMetrics for publishing custom CloudWatch metrics via EMF
metrics = EphemeralMetrics(namespace="lumi/Orchestrator")


@tracer.capture_lambda_handler
@logger.inject_lambda_context
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Orchestrate daily brief generation - single-GM or sweep-all mode.

    Supports two invocation modes:
    - generate-single: Per-GM EventBridge Scheduler fires with gmAlias and
      propertyId. Generates one brief for one GM. (REQ-SCHED-4)
    - generate-all: Manual trigger or backward-compatible sweep. Scans all
      GM settings and generates briefs sequentially.

    Args:
        event: EventBridge Scheduler event payload. Expected shapes:
            Single: {"source": "scheduler", "action": "generate-single",
                     "gmAlias": "jsmith", "propertyId": "ALOHA-CHI-001"}
            Sweep:  {"source": "scheduler", "action": "generate-all"}
            Legacy: {"propertyId": "ALOHA-CHI-001"} (backward compat)
        context: Lambda context object (used by Powertools decorator).

    Returns:
        Dict with pipeline execution status and brief metadata.
    """
    action = event.get("action", "")

    if action == "generate-single":
        # Per-GM schedule invocation (REQ-SCHED-4)
        property_id = event.get("propertyId", "")
        gm_alias = event.get("gmAlias", "")

        if not property_id:
            return {"statusCode": 400, "error": "propertyId required for generate-single"}

        logger.info(
            "Single-GM brief generation triggered",
            extra={"gm_alias": gm_alias, "property_id": property_id},
        )
        return _run_pipeline(property_id, gm_alias)

    elif action == "generate-all":
        # Manual sweep trigger - generate briefs for all GMs sequentially
        logger.info("Sweep-all brief generation triggered")
        return _run_sweep()

    else:
        # Legacy format: direct propertyId in event (backward compatibility)
        property_id = _extract_property_id_legacy(event)
        if property_id:
            logger.info(
                "Legacy single-property invocation",
                extra={"property_id": property_id},
            )
            return _run_pipeline(property_id)

        return {"statusCode": 400, "error": "Invalid event payload - missing action or propertyId"}


def _run_sweep() -> Dict[str, Any]:
    """Generate briefs for all GMs by scanning the settings table.

    Iterates through all GM settings and runs the pipeline for each.
    Failures for individual GMs are logged but do not stop the sweep.

    Returns:
        Summary dict with success/failure counts and per-GM results.
    """
    start_time = time.time()

    # Scan all settings (acceptable at pilot scale - 20 items)
    settings_table = _dynamodb_resource.Table(SETTINGS_TABLE_NAME)
    response = settings_table.scan()
    all_settings = response.get("Items", [])

    logger.info(
        "Starting sweep-all generation",
        extra={"gm_count": len(all_settings)},
    )

    results: List[Dict[str, Any]] = []
    success_count = 0
    failure_count = 0

    for settings in all_settings:
        property_id = settings.get("propertyId", "")
        gm_alias = settings.get("gmAlias", "unknown")

        if not property_id:
            logger.warning("Skipping GM with no propertyId", extra={"gm_alias": gm_alias})
            continue

        result = _run_pipeline(property_id, gm_alias)
        result["gmAlias"] = gm_alias
        results.append(result)

        if result.get("statusCode") == 200:
            success_count += 1
        else:
            failure_count += 1

    duration_ms = (time.time() - start_time) * 1000

    logger.info(
        "Sweep-all generation complete",
        extra={
            "total_gms": len(all_settings),
            "success_count": success_count,
            "failure_count": failure_count,
            "duration_ms": round(duration_ms, 2),
        },
    )

    return {
        "statusCode": 200,
        "action": "generate-all",
        "totalGms": len(all_settings),
        "successCount": success_count,
        "failureCount": failure_count,
        "durationMs": round(duration_ms, 2),
        "results": results,
    }


def _run_pipeline(property_id: str, gm_alias: str = "") -> Dict[str, Any]:
    """Execute the full brief generation pipeline for a single property.

    Pipeline stages:
    1. Read GM settings from DynamoDB
    2. Pull property data from SPOG/MDP APIs
    3. Prioritize action items
    4. Generate AI narrative via Bedrock
    5. Validate narrative against source data (retry if fails)
    6. Synthesize audio via Polly
    7. Write complete brief to DynamoDB

    Args:
        property_id: The property identifier to generate a brief for.
        gm_alias: The GM's alias (partition key for settings lookup).

    Returns:
        Dict with pipeline execution status and brief metadata.
    """
    start_time = time.time()

    logger.info(
        "Starting brief generation pipeline",
        property_id=property_id,
    )

    try:
        # Step 1: Read GM settings from DynamoDB (O(1) GetItem by gmAlias)
        settings = _read_gm_settings(property_id, gm_alias)
        gm_alias = settings.get("gmAlias", "unknown")

        logger.info(
            "GM settings loaded",
            property_id=property_id,
            gm_alias=gm_alias,
            language=settings.get("audioPreferences", {}).get("language", "en-US"),
        )

        # Add searchable annotations for X-Ray trace filtering
        tracer.put_annotation("propertyId", property_id)
        tracer.put_annotation("gmAlias", gm_alias)
        tracer.put_annotation("language", settings.get("audioPreferences", {}).get("language", "en-US"))

        # Step 2: Pull property data from SPOG/MDP
        raw_data = _pull_data_with_fallback(property_id, settings)

        # Step 3: Prioritize action items
        prioritized_actions = prioritize_actions(raw_data)
        raw_data["actionItems"] = prioritized_actions

        # Step 4 + 5: Generate and validate narrative (with retry)
        narrative = _generate_and_validate_narrative(raw_data, settings)

        # Step 6: Synthesize audio
        language = settings.get("audioPreferences", {}).get("language", "en-US")
        today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        audio_metadata = synthesize_audio(narrative, language, property_id, today_str)

        # Add audio synthesis result to trace metadata
        tracer.put_metadata("audio_status", audio_metadata.get("status"))

        # Step 7: Write complete brief to DynamoDB
        brief_record = _build_brief_record(
            property_id, raw_data, narrative, audio_metadata, settings
        )
        _write_brief_to_dynamodb(brief_record)

        # Publish success metric
        duration_ms = (time.time() - start_time) * 1000
        metrics.add_metric(name="BriefGenerationDuration", unit=MetricUnit.Milliseconds, value=duration_ms)
        metrics.add_metric(name="BriefGenerationSuccess", unit=MetricUnit.Count, value=1)
        metrics.flush_metrics()

        # Add pipeline duration to trace metadata
        tracer.put_metadata("pipeline_duration_ms", round(duration_ms, 2))

        logger.info(
            "Brief generation pipeline complete",
            property_id=property_id,
            gm_alias=gm_alias,
            audio_status=audio_metadata.get("status"),
            duration_ms=round(duration_ms, 2),
        )

        return {
            "statusCode": 200,
            "propertyId": property_id,
            "briefId": audio_metadata.get("briefId", ""),
            "audioStatus": audio_metadata.get("status", "TEXT_ONLY"),
            "durationMs": round(duration_ms, 2),
        }

    except AllSourcesFailedError:
        # All data sources failed - try to serve cached brief
        logger.error(
            "All data sources failed - attempting cached brief fallback",
            property_id=property_id,
        )
        metrics.add_metric(name="BriefGenerationFailure", unit=MetricUnit.Count, value=1)
        metrics.add_metric(name="AllSourcesFailure", unit=MetricUnit.Count, value=1)
        metrics.flush_metrics()

        return _serve_cached_brief(property_id)

    except Exception as error:
        # Unexpected pipeline failure - log and publish failure metric
        duration_ms = (time.time() - start_time) * 1000
        logger.exception(
            "Unexpected pipeline failure",
            property_id=property_id,
            error=str(error),
            duration_ms=round(duration_ms, 2),
        )
        metrics.add_metric(name="BriefGenerationFailure", unit=MetricUnit.Count, value=1)
        metrics.flush_metrics()

        return {
            "statusCode": 500,
            "propertyId": property_id,
            "error": str(error),
        }


def _extract_property_id_legacy(event: Dict[str, Any]) -> str:
    """Extract propertyId from legacy event formats (backward compatibility).

    Supports:
    - Direct: {"propertyId": "ALOHA-CHI-001"}
    - EventBridge detail: {"detail": {"propertyId": "ALOHA-CHI-001"}}

    Args:
        event: The Lambda event payload.

    Returns:
        The propertyId string, or empty string if not found.
    """
    # Try EventBridge detail format
    property_id = event.get("detail", {}).get("propertyId", "")

    # Fall back to direct format
    if not property_id:
        property_id = event.get("propertyId", "")

    return property_id


def _read_gm_settings(property_id: str, gm_alias: str = "") -> Dict[str, Any]:
    """Read GM settings from DynamoDB stayos-settings table.

    Uses a direct GetItem by gmAlias (partition key) for O(1) lookup
    when gm_alias is provided. Falls back to defaults if the item is
    not found or gm_alias is empty.

    Args:
        property_id: The property identifier (used for default settings).
        gm_alias: The GM's alias - partition key of stayos-settings table.

    Returns:
        GM settings dict from DynamoDB (converted from Decimal types).
    """
    if not gm_alias:
        logger.warning(
            "No gmAlias provided for settings lookup - using defaults",
            property_id=property_id,
        )
        return _get_default_settings(property_id)

    settings_table_name = os.environ.get("SETTINGS_TABLE_NAME", SETTINGS_TABLE_NAME)
    table = _dynamodb_resource.Table(settings_table_name)

    try:
        # Direct key lookup - gmAlias is the sole partition key of stayos-settings
        response = table.get_item(Key={"gmAlias": gm_alias})

        item = response.get("Item")
        if not item:
            logger.warning(
                "No GM settings found for alias - using defaults",
                property_id=property_id,
                gm_alias=gm_alias,
            )
            return _get_default_settings(property_id)

        logger.info(
            "GM settings retrieved",
            property_id=property_id,
            gm_alias=gm_alias,
        )
        return item

    except ClientError as error:
        logger.error(
            "Failed to read GM settings from DynamoDB",
            property_id=property_id,
            gm_alias=gm_alias,
            error=str(error),
        )
        # Return defaults on error so pipeline can continue
        return _get_default_settings(property_id)


def _get_default_settings(property_id: str) -> Dict[str, Any]:
    """Return default GM settings when none are configured.

    Provides safe defaults so the pipeline can still generate a brief
    even without explicit GM configuration.

    Args:
        property_id: The property identifier.

    Returns:
        Default settings dict with English language and standard preferences.
    """
    return {
        "gmAlias": "default",
        "gmName": "General Manager",
        "propertyId": property_id,
        "propertyName": "Property",
        "audioPreferences": {
            "language": "en-US",
            "briefLength": "standard",
        },
        "alertToggles": {
            "overbookingRisk": True,
            "roomsOutOfOrder": True,
            "vipArrivalAlert": True,
            "upsellOpportunity": True,
            "staffingConfirmed": True,
        },
    }


@tracer.capture_method
def _pull_data_with_fallback(
    property_id: str, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Pull property data, letting AllSourcesFailedError propagate.

    The caller handles AllSourcesFailedError to serve a cached brief.
    PartialDataError is handled here - a partial brief is acceptable.

    Args:
        property_id: The property identifier.
        settings: GM settings dict.

    Returns:
        Combined property data from available sources.

    Raises:
        AllSourcesFailedError: When all data sources are unavailable.
    """
    return pull_property_data(property_id, settings)


@tracer.capture_method
def _generate_and_validate_narrative(
    raw_data: Dict[str, Any], settings: Dict[str, Any]
) -> str:
    """Generate narrative via Bedrock and validate against source data.

    Implements the retry logic: if validation fails on first attempt,
    regenerates with the same prompt (one retry). If still invalid,
    uses the template fallback from brief_generator.

    Args:
        raw_data: Combined property data with prioritized actions.
        settings: GM settings dict.

    Returns:
        Validated narrative string (AI-generated or template fallback).
    """
    max_attempts = 2

    for attempt in range(max_attempts):
        try:
            narrative = generate_brief_narrative(raw_data, settings)

            # Validate the narrative against source KPI data
            is_valid, discrepancies = validate_narrative(narrative, raw_data)

            if is_valid:
                logger.info(
                    "Narrative validated successfully",
                    attempt=attempt + 1,
                )
                return narrative

            # Validation failed - log discrepancies
            logger.warning(
                "Narrative validation failed",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                discrepancies=discrepancies,
            )

        except BriefGenerationError as error:
            logger.error(
                "Brief generation error during validation loop",
                attempt=attempt + 1,
                error=str(error),
            )

    # All attempts exhausted - the brief_generator already returns a fallback
    # narrative from its internal logic, so this path should rarely be hit.
    logger.warning(
        "Validation failed after all attempts - using last generated narrative",
    )
    return generate_brief_narrative(raw_data, settings)


def _build_brief_record(
    property_id: str,
    raw_data: Dict[str, Any],
    narrative: str,
    audio_metadata: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Construct the complete brief record for DynamoDB storage.

    Assembles all pipeline outputs into a single record matching the
    stayos-briefs table schema with TTL set to 30 days from now.

    Args:
        property_id: The property identifier (partition key).
        raw_data: Combined property data with KPIs and action items.
        narrative: The validated narrative text.
        audio_metadata: Audio synthesis result metadata.
        settings: GM settings used for this generation.

    Returns:
        Complete brief record dict ready for DynamoDB put_item.
    """
    now = datetime.now(tz=timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    # Calculate TTL as epoch seconds (30 days from now)
    ttl_epoch = int(now.timestamp()) + TTL_SECONDS

    return {
        "propertyId": property_id,
        "briefDate": today_str,
        "generatedAt": now_iso,
        "asOf": raw_data.get("dailyKPIs", {}).get("asOf", now_iso),
        "property": raw_data.get("property", {}),
        "dailyKPIs": raw_data.get("dailyKPIs", {}),
        "actionItems": raw_data.get("actionItems", []),
        "vipArrivals": raw_data.get("vipArrivals", []),
        "narrative": narrative,
        "audioBrief": {
            "briefId": audio_metadata.get("briefId", ""),
            "status": audio_metadata.get("status", "TEXT_ONLY"),
            "durationSeconds": audio_metadata.get("durationSeconds", 0),
            "s3Key": audio_metadata.get("s3Key"),
            "cloudFrontUrl": audio_metadata.get("cloudFrontUrl"),
            "voiceId": audio_metadata.get("voiceId", ""),
            "engine": audio_metadata.get("engine", ""),
        },
        "dataSourceStatus": raw_data.get("dataSourceStatus", {}),
        "gmAlias": settings.get("gmAlias", "unknown"),
        "language": settings.get("audioPreferences", {}).get("language", "en-US"),
        "ttl": ttl_epoch,
    }


def _convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB's boto3 interface does not accept Python float types.
    This converts all floats in nested dicts/lists to Decimal.

    Args:
        obj: Any Python object (dict, list, float, int, str, etc.)

    Returns:
        The same structure with all floats replaced by Decimals.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {key: _convert_floats_to_decimal(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_convert_floats_to_decimal(item) for item in obj]
    return obj


@tracer.capture_method
def _write_brief_to_dynamodb(brief_record: Dict[str, Any]) -> None:
    """Write the complete brief record to DynamoDB stayos-briefs table.

    Uses put_item to create or overwrite the daily brief for a property.

    Args:
        brief_record: Complete brief record dict to store.

    Raises:
        ClientError: If the DynamoDB write fails (logged and re-raised).
    """
    briefs_table_name = os.environ.get("BRIEFS_TABLE_NAME", BRIEFS_TABLE_NAME)
    table = _dynamodb_resource.Table(briefs_table_name)

    try:
        # Convert float values to Decimal (DynamoDB does not accept Python floats)
        converted_record = _convert_floats_to_decimal(brief_record)
        table.put_item(Item=converted_record)

        logger.info(
            "Brief written to DynamoDB",
            property_id=brief_record.get("propertyId"),
            brief_date=brief_record.get("briefDate"),
            audio_status=brief_record.get("audioBrief", {}).get("status"),
        )

    except ClientError as error:
        logger.error(
            "Failed to write brief to DynamoDB",
            property_id=brief_record.get("propertyId"),
            error=str(error),
        )
        raise


def _serve_cached_brief(property_id: str) -> Dict[str, Any]:
    """Serve the most recent cached brief when all data sources fail.

    Reads the latest brief from DynamoDB and returns it with a
    stale-data indicator so the frontend can warn the GM.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with the cached brief or error if no cache exists.
    """
    briefs_table_name = os.environ.get("BRIEFS_TABLE_NAME", BRIEFS_TABLE_NAME)
    table = _dynamodb_resource.Table(briefs_table_name)

    try:
        # Query for the most recent brief for this property
        response = table.query(
            KeyConditionExpression="propertyId = :pid",
            ExpressionAttributeValues={":pid": property_id},
            ScanIndexForward=False,
            Limit=1,
        )

        items = response.get("Items", [])
        if items:
            logger.info(
                "Serving cached brief due to data source failure",
                property_id=property_id,
                cached_date=items[0].get("briefDate"),
            )
            return {
                "statusCode": 200,
                "propertyId": property_id,
                "cached": True,
                "briefDate": items[0].get("briefDate"),
                "message": "Serving cached brief - data sources unavailable",
            }

        logger.error(
            "No cached brief available",
            property_id=property_id,
        )
        return {
            "statusCode": 503,
            "propertyId": property_id,
            "error": "All data sources failed and no cached brief available",
        }

    except ClientError as error:
        logger.error(
            "Failed to read cached brief from DynamoDB",
            property_id=property_id,
            error=str(error),
        )
        return {
            "statusCode": 503,
            "propertyId": property_id,
            "error": f"All data sources failed and cache read failed: {error}",
        }
