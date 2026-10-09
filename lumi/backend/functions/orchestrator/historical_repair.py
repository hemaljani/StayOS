"""Repair existing briefs from same-date revenues without backdating live data.

Deployment automation backs up each original record and audio first. A snapshot
fingerprint and conditional write prevent a repair from overwriting later work.
"""

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import time

from boto3.dynamodb.conditions import Attr, Key

from data_validator import validate_narrative
from revenue_kpis import format_revenue_kpis


def snapshot_fingerprint(value):
    """Hash native DynamoDB values consistently across JSON/Decimal boundaries."""

    def canonical(item):
        if isinstance(item, (Decimal, float, int)) and not isinstance(item, bool):
            return {"number": format(Decimal(str(item)).normalize(), "f")}
        if isinstance(item, dict):
            return {key: canonical(val) for key, val in sorted(item.items())}
        if isinstance(item, list):
            return [canonical(val) for val in item]
        return item

    payload = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def decimal_record(value):
    """Convert fractional output to the exact representation DynamoDB accepts."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: decimal_record(val) for key, val in value.items()}
    if isinstance(value, list):
        return [decimal_record(val) for val in value]
    return value


def repair_existing_brief(event, dynamodb, read_settings, generate, synthesize):
    """Repair one backed-up snapshot; return metadata without guest contents."""
    property_id = event.get("propertyId", "")
    brief_date = event.get("briefDate", "")
    run_id = event.get("repairRunId", "")
    if (
        not re.fullmatch(r"ALOHA-[A-Z]{3}-001", property_id)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id)
        or not event.get("backupVerified")
        or not re.fullmatch(r"[a-f0-9]{64}", event.get("expectedFingerprint", ""))
        or not re.fullmatch(r"[a-f0-9]{64}", event.get("revenueFingerprint", ""))
    ):
        return {"statusCode": 400, "status": "INVALID_REPAIR_REQUEST"}
    try:
        parsed_date = date.fromisoformat(brief_date)
    except ValueError:
        return {"statusCode": 400, "status": "INVALID_DATE"}
    if parsed_date > datetime.now(timezone.utc).date():
        return {"statusCode": 400, "status": "FUTURE_DATE"}

    table = dynamodb.Table(os.environ.get("BRIEFS_TABLE_NAME", "stayos-briefs"))
    revenues = dynamodb.Table(os.environ.get("REVENUES_TABLE_NAME", "stayos-revenues"))
    # The existing brief-writer role grants Query, not GetItem. Read exactly
    # this property/date through its current permission; no IAM expansion.
    items = table.query(
        KeyConditionExpression=Key("propertyId").eq(property_id)
        & Key("briefDate").eq(brief_date),
        ConsistentRead=True,
        Limit=1,
    ).get("Items", [])
    original = items[0] if items else None
    if not original or int(original.get("ttl", 0)) <= time.time():
        return {"statusCode": 200, "status": "SKIPPED_EXPIRED_OR_REMOVED"}
    if snapshot_fingerprint(original) != event["expectedFingerprint"]:
        return {"statusCode": 409, "status": "SNAPSHOT_CHANGED"}
    source_key = {"propertyId": property_id, "date": brief_date}
    revenue = revenues.get_item(Key=source_key, ConsistentRead=True).get("Item")
    if not revenue:
        return {"statusCode": 422, "status": "MISSING_REVENUE"}
    if snapshot_fingerprint(revenue) != event["revenueFingerprint"]:
        return {"statusCode": 409, "status": "REVENUE_CHANGED"}

    # Use the original VIP/actions snapshot, never today's operational lists.
    raw_data = copy.deepcopy(original)
    counts = {
        key: int(original.get("dailyKPIs", {}).get("arrivals", {}).get(key, 0))
        for key in ("vipCount", "ambassadorCount", "titaniumCount", "platinumCount")
    }
    as_of = original.get("asOf") or original.get("dailyKPIs", {}).get("asOf")
    raw_data["dailyKPIs"] = format_revenue_kpis(
        revenue, brief_date, as_of or f"{brief_date}T00:00:00+00:00", counts
    )
    settings = read_settings(property_id, original.get("gmAlias", ""))
    settings = copy.deepcopy(settings)
    settings.setdefault("audioPreferences", {})["language"] = original.get(
        "language", settings.get("audioPreferences", {}).get("language", "en-US")
    )
    # Date components are legitimate narrative numbers, not hallucinated KPIs.
    validation_data = json.loads(json.dumps(raw_data, default=float))
    validation_data["briefDateComponents"] = [
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
    ]
    narrative = generate(validation_data, settings)
    valid, discrepancies = validate_narrative(
        narrative,
        validation_data,
        language=settings["audioPreferences"]["language"],
    )
    if not narrative.strip() or not valid:
        return {
            "statusCode": 422,
            "status": "NARRATIVE_INVALID",
            "discrepancyCount": len(discrepancies),
        }
    audio = synthesize(
        narrative,
        settings["audioPreferences"]["language"],
        property_id,
        brief_date,
        key_suffix=run_id,
    )
    if audio.get("status") != "READY" or not audio.get("s3Key"):
        return {"statusCode": 502, "status": "AUDIO_NOT_READY"}
    if (
        snapshot_fingerprint(
            revenues.get_item(Key=source_key, ConsistentRead=True).get("Item")
        )
        != event["revenueFingerprint"]
    ):
        return {"statusCode": 409, "status": "REVENUE_CHANGED"}

    repaired = copy.deepcopy(original)
    repaired["dailyKPIs"] = raw_data["dailyKPIs"]
    repaired["narrative"] = narrative
    repaired["audioBrief"] = {
        key: val for key, val in audio.items() if key != "briefId"
    }
    repaired["audioBrief"]["briefId"] = audio["briefId"]
    repaired["dataSourceStatus"] = {"DATASET_REVENUES": "SUCCESS"}
    repaired["generatedAt"] = datetime.now(timezone.utc).isoformat()
    repaired["repairMetadata"] = {
        "runId": run_id,
        "originalFingerprint": event["expectedFingerprint"],
        "revenueFingerprint": event["revenueFingerprint"],
    }
    condition = Attr("propertyId").exists() & Attr("ttl").gt(int(time.time()))
    for field, value in original.items():
        condition &= Attr(field).eq(value)
    try:
        table.put_item(Item=decimal_record(repaired), ConditionExpression=condition)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return {"statusCode": 409, "status": "SNAPSHOT_CHANGED_OR_EXPIRED"}
    return {
        "statusCode": 200,
        "status": "REPAIRED",
        "propertyId": property_id,
        "briefDate": brief_date,
        "audioKey": audio["s3Key"],
        "fingerprint": snapshot_fingerprint(decimal_record(repaired)),
    }
