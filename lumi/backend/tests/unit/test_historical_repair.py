"""Exercise bounded repair against DynamoDB, including concurrent write refusal."""

from decimal import Decimal
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

import boto3
from moto import mock_aws
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "functions/orchestrator"))
from historical_repair import repair_existing_brief, snapshot_fingerprint  # noqa: E402


@pytest.fixture
def repair_case(monkeypatch):
    monkeypatch.setenv("REVENUES_TABLE_NAME", "repair-revenues")
    monkeypatch.setenv("BRIEFS_TABLE_NAME", "repair-briefs")
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        for name, sort_key in (
            ("repair-revenues", "date"),
            ("repair-briefs", "briefDate"),
        ):
            db.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": sort_key, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "propertyId", "AttributeType": "S"},
                    {"AttributeName": sort_key, "AttributeType": "S"},
                ],
            )
        original = {
            "propertyId": "ALOHA-CHI-001",
            "briefDate": "2026-10-01",
            "ttl": int(time.time()) + 86400,
            "generatedAt": "2026-10-01T11:00:00Z",
            "gmAlias": "demo",
            "language": "en-US",
            "property": {"propertyId": "ALOHA-CHI-001", "propertyName": "Chicago"},
            "dailyKPIs": {"arrivals": {"vipCount": 2}, "asOf": "2026-10-01T11:00:00Z"},
            "actionItems": [{"title": "Historical action", "severity": "HIGH"}],
            "vipArrivals": [{"guestId": "historical-guest"}],
            "narrative": "Old content",
            "audioBrief": {"s3Key": "old.mp3"},
        }
        revenue = {
            "propertyId": original["propertyId"],
            "date": original["briefDate"],
            "occupancyPct": Decimal("90.5"),
            "adr": Decimal("250.75"),
            "revpar": Decimal("226.25"),
            "availableRooms": 368,
        }
        db.Table("repair-briefs").put_item(Item=original)
        db.Table("repair-revenues").put_item(Item=revenue)
        event = {
            "propertyId": original["propertyId"],
            "briefDate": original["briefDate"],
            "repairRunId": "test-run",
            "backupVerified": True,
            "expectedFingerprint": snapshot_fingerprint(original),
            "revenueFingerprint": snapshot_fingerprint(revenue),
        }
        settings = Mock(return_value={"audioPreferences": {"language": "en-US"}})
        generate = Mock(return_value="Occupancy 90.5%, ADR 250.75, RevPAR 226.25.")
        audio = Mock(
            return_value={
                "status": "READY",
                "briefId": "new",
                "s3Key": "new-test-run.mp3",
            }
        )
        yield db, original, revenue, event, settings, generate, audio


def execute(case, event=None):
    db, _, _, request, settings, generate, audio = case
    return repair_existing_brief(event or request, db, settings, generate, audio)


def stored(case):
    db, original, *_ = case
    return db.Table("repair-briefs").get_item(
        Key={key: original[key] for key in ("propertyId", "briefDate")}
    )["Item"]


def test_preserves_historical_snapshot_ttl_and_fractional_values(repair_case):
    assert execute(repair_case)["status"] == "REPAIRED"
    updated = stored(repair_case)
    for field in (
        "propertyId",
        "briefDate",
        "ttl",
        "vipArrivals",
        "actionItems",
        "property",
    ):
        assert updated[field] == repair_case[1][field]
    assert updated["dailyKPIs"]["date"] == "2026-10-01"
    assert updated["dailyKPIs"]["occupancy"]["current"] == Decimal("90.5")
    assert updated["dailyKPIs"]["adr"]["current"] == Decimal("250.75")
    assert updated["dailyKPIs"]["revPAR"]["current"] == Decimal("226.25")
    assert updated["audioBrief"]["s3Key"] != repair_case[1]["audioBrief"]["s3Key"]
    repair_case[-1].assert_called_once()
    assert repair_case[-1].call_args.kwargs == {"key_suffix": "test-run"}
    assert (
        repair_case[-2].call_args.args[0]["vipArrivals"]
        == repair_case[1]["vipArrivals"]
    )


def test_reads_brief_with_existing_query_permission(repair_case):
    """The deployed writer role has Query but does not grant brief GetItem."""
    db, _, _, event, settings, generate, audio = repair_case
    table_factory = db.Table
    brief_table = table_factory("repair-briefs")
    brief_table.get_item = Mock(side_effect=AssertionError("GetItem is not allowed"))
    query = Mock(wraps=brief_table.query)
    brief_table.query = query
    with patch.object(
        db,
        "Table",
        side_effect=lambda name: (
            brief_table if name == "repair-briefs" else table_factory(name)
        ),
    ):
        assert (
            repair_existing_brief(event, db, settings, generate, audio)["status"]
            == "REPAIRED"
        )
    brief_table.get_item.assert_not_called()
    assert query.call_args.kwargs["ConsistentRead"] is True
    assert query.call_args.kwargs["Limit"] == 1


def test_spanish_repair_validates_decimal_commas_without_changing_narration(
    repair_case,
):
    db, original, _, event, _, generate, audio = repair_case
    original["language"] = "es-ES"
    db.Table("repair-briefs").put_item(Item=original)
    event["expectedFingerprint"] = snapshot_fingerprint(original)
    generate.return_value = "Ocupación 90,5%, ADR 250,75, RevPAR 226,25."
    assert execute(repair_case)["status"] == "REPAIRED"
    assert stored(repair_case)["narrative"] == generate.return_value
    assert audio.call_args.args[1] == "es-ES"


@pytest.mark.parametrize(
    "field,value",
    [
        ("backupVerified", False),
        ("repairRunId", "../unsafe"),
        ("expectedFingerprint", ""),
        ("briefDate", "invalid"),
        ("briefDate", "2099-01-01"),
        ("propertyId", "another-property"),
    ],
)
def test_rejects_invalid_scope_before_generation(repair_case, field, value):
    event = {**repair_case[3], field: value}
    assert execute(repair_case, event)["statusCode"] == 400
    repair_case[-2].assert_not_called()


def test_expired_records_are_never_recreated_or_extended(repair_case):
    db, original, *_ = repair_case
    expired = {**original, "ttl": int(time.time()) - 1}
    db.Table("repair-briefs").put_item(Item=expired)
    assert execute(repair_case)["status"] == "SKIPPED_EXPIRED_OR_REMOVED"
    assert stored(repair_case) == expired


def test_missing_or_changed_revenue_refuses_repair(repair_case):
    db, _, revenue, *_ = repair_case
    db.Table("repair-revenues").delete_item(
        Key={key: revenue[key] for key in ("propertyId", "date")}
    )
    assert execute(repair_case)["status"] == "MISSING_REVENUE"
    repair_case[-2].assert_not_called()


def test_concurrent_historical_action_change_is_preserved(repair_case):
    db, original, _, _, _, _, audio = repair_case
    changed = {**original, "actionItems": [{"title": "Concurrent edit"}]}

    def concurrent(*args, **kwargs):
        db.Table("repair-briefs").put_item(Item=changed)
        return {"status": "READY", "briefId": "new", "s3Key": "new.mp3"}

    audio.side_effect = concurrent
    assert execute(repair_case)["status"] == "SNAPSHOT_CHANGED_OR_EXPIRED"
    assert stored(repair_case) == changed


@pytest.mark.parametrize("narrative", ["", "Occupancy 999999."])
def test_invalid_narrative_cannot_replace_record(repair_case, narrative):
    repair_case[-2].return_value = narrative
    assert execute(repair_case)["status"] == "NARRATIVE_INVALID"
    assert stored(repair_case) == repair_case[1]
    repair_case[-1].assert_not_called()


def test_audio_failure_preserves_prior_record(repair_case):
    repair_case[-1].return_value = {"status": "TEXT_ONLY"}
    assert execute(repair_case)["status"] == "AUDIO_NOT_READY"
    assert stored(repair_case) == repair_case[1]


def test_narrative_prompt_and_fallback_use_source_day():
    from brief_generator import _fill_template_variables, _generate_fallback_narrative

    raw = {"dailyKPIs": {"date": "2026-09-09"}}
    assert "September 09, 2026" in _fill_template_variables("{date}", raw, {})
    assert "September 09, 2026" in _generate_fallback_narrative(raw, {}, "en-US")


def test_fingerprint_is_stable_across_decimal_json_boundary():
    assert snapshot_fingerprint({"x": Decimal("90.50")}) == snapshot_fingerprint(
        {"x": 90.5}
    )


def test_repair_audio_has_unique_path_and_input_hash(monkeypatch):
    """Tie repaired MP3 metadata to the exact validated Polly input."""
    import hashlib
    import audio_synthesizer

    upload = Mock()
    monkeypatch.setattr(audio_synthesizer, "_call_polly", Mock(return_value=b"mp3"))
    monkeypatch.setattr(audio_synthesizer, "_upload_to_s3", upload)
    result = audio_synthesizer.synthesize_audio(
        "Occupancy 90.5%.",
        "en-US",
        "ALOHA-CHI-001",
        "2026-10-01",
        key_suffix="test-run",
    )
    assert result["s3Key"].endswith("morning-brief-test-run.mp3")
    assert upload.call_args.kwargs == {
        "narrative_hash": hashlib.sha256(b"Occupancy 90.5%.").hexdigest()
    }
