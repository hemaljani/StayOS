"""Unit tests for the RegenerateBrief step: ordering guard + generate-single.

Covers Task 6 for the data-Orchestrator spec:

* Brief_Ordering_Guard warns and returns ``False`` when a property's brief cron
  would fire before the Roll-Forward completes (Requirements 3.3, 3.4), and
  holds when completion precedes the next cron.
* Brief regeneration invokes the LUMI orchestrator ``generate-single`` via a
  boto3 Lambda ``Invoke`` with the correct ``{gmAlias, propertyId}`` payload,
  mirroring ``schedule_manager`` (Requirements 4.1, 4.2).
* A brief failure is logged and surfaced in the result envelope without raising
  (so written operational data is not rolled back, Requirement 4.3).

External boundaries (boto3 Lambda invoke, DynamoDB settings read) are mocked;
no AWS is contacted.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import regenerate_brief_handler as rbh
from orchestrator_common import MODE_ROLL_FORWARD, MODE_SEED, STATUS_FAILED

from conftest import make_lambda_context

CTX = make_lambda_context()

# A Chicago property GM brief cron: fires daily at 06:30 America/Chicago.
CHICAGO_SCHEDULE = rbh.PropertySchedule(
    gm_alias="jsmith",
    property_id="ALOHA-CHI-001",
    delivery_time="06:30",
    timezone="America/Chicago",
)

CHICAGO = ZoneInfo("America/Chicago")

ROLL_FORWARD_EVENT = {
    "mode": MODE_ROLL_FORWARD,
    "propertyId": "ALOHA-CHI-001",
    "referenceDate": "2026-08-17",
}


class _FakeLambdaClient:
    """Fake Lambda client recording invocations and returning a canned result."""

    def __init__(self, status_code: int = 200, function_error: str | None = None) -> None:
        self.calls: list[dict] = []
        self._status_code = status_code
        self._function_error = function_error

    def invoke(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        body = json.dumps({"statusCode": self._status_code}).encode("utf-8")
        response: dict = {"Payload": _FakePayload(body)}
        if self._function_error:
            response["FunctionError"] = self._function_error
        return response


class _FakePayload:
    """Stand-in for the streaming body returned by Lambda invoke."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture()
def brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env vars the RegenerateBrief step reads (function name + table)."""
    monkeypatch.setenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", "stayos-orchestrator")
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "stayos-settings-test")


def _wire(monkeypatch: pytest.MonkeyPatch, client: _FakeLambdaClient, schedule) -> None:
    """Point the handler at the fake Lambda client and a fixed schedule."""
    monkeypatch.setattr(rbh, "_get_lambda_client", lambda: client)
    monkeypatch.setattr(rbh, "_read_property_schedule", lambda property_id: schedule)


# --- Ordering guard -----------------------------------------------------------


class TestOrderingGuard:
    """Brief_Ordering_Guard computed from IANA timezone + delivery_time."""

    def test_guard_holds_when_completion_precedes_next_cron(self) -> None:
        # Completes at 00:05 local; next 06:30 cron is later the same day.
        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        assert rbh.is_ordering_guard_satisfied(completion, "06:30", "America/Chicago") is True

    def test_guard_holds_at_exact_earlier_completion(self) -> None:
        # Completes at 05:00 local; the 06:30 cron is still ahead -> guard holds.
        completion = datetime(2026, 8, 17, 5, 0, tzinfo=CHICAGO)
        assert rbh.is_ordering_guard_satisfied(completion, "06:30", "America/Chicago") is True

    def test_check_ordering_guard_warns_on_breach(self, caplog) -> None:
        # cron at 06:00 local, completion at 06:15 local -> brief cron precedes
        # completion, i.e. the guard is breached and a warning must be logged.
        completion = datetime(2026, 8, 17, 6, 15, tzinfo=CHICAGO)
        schedule = rbh.PropertySchedule("jsmith", "ALOHA-CHI-001", "06:00", "America/Chicago")
        with caplog.at_level(logging.WARNING):
            ok = rbh.check_ordering_guard(schedule, completion)
        assert ok is False
        # Warning identifies the property (Requirement 3.4).
        assert any("ALOHA-CHI-001" in rec.getMessage() or
                   "ALOHA-CHI-001" in str(getattr(rec, "propertyId", "")) or
                   "Brief_Ordering_Guard breach" in rec.getMessage()
                   for rec in caplog.records)

    def test_guard_uses_property_local_timezone(self) -> None:
        # 22:00 UTC on 2026-08-17 is 07:00 JST on 2026-08-18 in Tokyo. With a
        # 06:30 Tokyo cron, that morning's brief already fired -> breach.
        completion_utc = datetime(2026, 8, 17, 22, 0, tzinfo=ZoneInfo("UTC"))
        schedule = rbh.PropertySchedule("ttanaka", "ALOHA-TYO-001", "06:30", "Asia/Tokyo")
        assert rbh.check_ordering_guard(schedule, completion_utc) is False


# --- Settings scan pagination (CR-3) -----------------------------------------


class _PaginatedSettingsTable:
    """Fake DynamoDB Table whose scan matches only on a LATER page.

    Models the CR-3 failure mode: a Scan ``FilterExpression`` is applied after
    each 1 MB page, so page 1 can come back with zero ``Items`` and a
    ``LastEvaluatedKey`` while the matching row sits on page 2. The old
    single-scan code returned page 1 only and wrongly concluded "no settings".
    """

    def __init__(self, match_item: dict) -> None:
        self._match_item = match_item
        self.scan_calls = 0

    def scan(self, **kwargs):
        self.scan_calls += 1
        if "ExclusiveStartKey" not in kwargs:
            # Page 1: filter matched nothing on this page, but more pages exist.
            return {"Items": [], "LastEvaluatedKey": {"gmAlias": {"S": "cursor"}}}
        # Page 2: the matching row.
        return {"Items": [self._match_item]}


def test_read_property_schedule_finds_match_on_later_page(
    brief_env, monkeypatch
) -> None:
    """_read_property_schedule paginates and finds a match beyond page 1 (CR-3)."""
    match = {
        "gmAlias": "jsmith",
        "propertyId": "ALOHA-CHI-001",
        "briefDeliveryTime": "06:30",
        "timezone": "America/Chicago",
    }
    fake_table = _PaginatedSettingsTable(match)
    monkeypatch.setattr(rbh, "_get_settings_table", lambda: fake_table)

    schedule = rbh._read_property_schedule("ALOHA-CHI-001")

    assert fake_table.scan_calls == 2  # it did NOT stop after the empty page 1
    assert schedule is not None
    assert schedule.gm_alias == "jsmith"
    assert schedule.property_id == "ALOHA-CHI-001"
    assert schedule.delivery_time == "06:30"
    assert schedule.timezone == "America/Chicago"


def test_read_property_schedule_none_when_no_page_matches(
    brief_env, monkeypatch
) -> None:
    """With no match on any page, the schedule resolves to None (not a crash)."""

    class _EmptyTable:
        def scan(self, **kwargs):
            return {"Items": []}

    monkeypatch.setattr(rbh, "_get_settings_table", lambda: _EmptyTable())
    assert rbh._read_property_schedule("ALOHA-CHI-001") is None


# --- generate-single invocation ----------------------------------------------


class TestGenerateSingleInvocation:
    """RegenerateBrief invokes LUMI generate-single with the right payload."""

    def test_invoked_with_correct_gm_alias_and_property(
        self, brief_env, monkeypatch
    ) -> None:
        client = _FakeLambdaClient(status_code=200)
        _wire(monkeypatch, client, CHICAGO_SCHEDULE)

        # Completion well before the 06:30 cron so the guard holds.
        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        step_input = rbh.parse_step_input(dict(ROLL_FORWARD_EVENT))
        details = rbh.regenerate_briefs(step_input, pilot_property_ids=[], completion=completion)

        assert details["briefsRequested"] == 1
        assert details["briefsFailed"] == 0
        assert len(client.calls) == 1

        call = client.calls[0]
        assert call["FunctionName"] == "stayos-orchestrator"
        assert call["InvocationType"] == "RequestResponse"
        payload = json.loads(call["Payload"].decode("utf-8"))
        assert payload == {
            "source": "scheduler",
            "action": "generate-single",
            "gmAlias": "jsmith",
            "propertyId": "ALOHA-CHI-001",
        }

    def test_brief_failure_is_surfaced_without_raising(
        self, brief_env, monkeypatch, caplog
    ) -> None:
        # LUMI returns a 500 -> brief failed. The step must NOT raise (so written
        # data is not rolled back, Requirement 4.3) but must report status failed.
        client = _FakeLambdaClient(status_code=500)
        _wire(monkeypatch, client, CHICAGO_SCHEDULE)

        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        step_input = rbh.parse_step_input(dict(ROLL_FORWARD_EVENT))

        with caplog.at_level(logging.ERROR):
            details = rbh.regenerate_briefs(step_input, pilot_property_ids=[], completion=completion)

        assert details["briefsFailed"] == 1
        assert details["failedProperties"] == ["ALOHA-CHI-001"]
        # The full handler reports a failed step status, not masked as success
        # (Requirement 9.3), while still returning a normal envelope (no raise,
        # so written data is not rolled back, Requirement 4.3).
        result = rbh.lambda_handler(dict(ROLL_FORWARD_EVENT), CTX)
        assert result["step"] == "RegenerateBrief"
        assert result["status"] == STATUS_FAILED
        assert result["details"]["briefsFailed"] == 1

    def test_function_error_is_surfaced_without_raising(
        self, brief_env, monkeypatch
    ) -> None:
        client = _FakeLambdaClient(status_code=200, function_error="Unhandled")
        _wire(monkeypatch, client, CHICAGO_SCHEDULE)

        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        step_input = rbh.parse_step_input(dict(ROLL_FORWARD_EVENT))
        details = rbh.regenerate_briefs(step_input, pilot_property_ids=[], completion=completion)

        assert details["briefsFailed"] == 1
        assert details["results"][0]["status"] == STATUS_FAILED

    def test_missing_gm_alias_is_a_failure_not_a_crash(
        self, brief_env, monkeypatch
    ) -> None:
        client = _FakeLambdaClient()
        # Schedule resolves to None (no settings for the property).
        _wire(monkeypatch, client, None)

        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        step_input = rbh.parse_step_input(dict(ROLL_FORWARD_EVENT))
        details = rbh.regenerate_briefs(step_input, pilot_property_ids=[], completion=completion)

        assert details["briefsFailed"] == 1
        # No invoke attempted when the gmAlias cannot be resolved.
        assert client.calls == []

    def test_malformed_delivery_time_isolated_to_its_property(
        self, brief_env, monkeypatch
    ) -> None:
        """One bad briefDeliveryTime must not abort the other properties (CR-4).

        The ordering-guard parse for a malformed ``briefDeliveryTime`` used to
        raise ``ValueError`` outside the per-property try, aborting every
        remaining property. It must now be isolated: the bad property is marked
        failed and the good property still gets its brief regenerated.
        """
        client = _FakeLambdaClient(status_code=200)

        good = rbh.PropertySchedule(
            "jsmith", "ALOHA-CHI-001", "06:30", "America/Chicago"
        )
        # "24:00" is a well-shaped-looking but invalid time (hour out of range),
        # exactly the kind of malformed settings value CR-4 calls out.
        bad = rbh.PropertySchedule(
            "bkhan", "ALOHA-NYC-001", "24:00", "America/New_York"
        )
        schedules = {"ALOHA-NYC-001": bad, "ALOHA-CHI-001": good}
        monkeypatch.setattr(rbh, "_get_lambda_client", lambda: client)
        monkeypatch.setattr(
            rbh, "_read_property_schedule", lambda property_id: schedules[property_id]
        )

        # Seed mode fans out over the full pilot list; the bad property is first
        # so, under the old code, its raise would skip the good one entirely.
        seed_event = {"mode": MODE_SEED}
        step_input = rbh.parse_step_input(dict(seed_event))
        completion = datetime(2026, 8, 17, 0, 5, tzinfo=CHICAGO)
        details = rbh.regenerate_briefs(
            step_input,
            pilot_property_ids=["ALOHA-NYC-001", "ALOHA-CHI-001"],
            completion=completion,
        )

        # Both properties were processed: the bad one failed, the good one ran.
        assert details["briefsRequested"] == 2
        assert details["briefsFailed"] == 1
        assert details["failedProperties"] == ["ALOHA-NYC-001"]
        assert details["briefsSucceeded"] == 1
        # The good property's brief was actually invoked despite the bad one.
        assert len(client.calls) == 1
        invoked_payload = json.loads(client.calls[0]["Payload"].decode("utf-8"))
        assert invoked_payload["propertyId"] == "ALOHA-CHI-001"
        # The failure reason names the malformed time.
        bad_result = next(
            r for r in details["results"] if r["propertyId"] == "ALOHA-NYC-001"
        )
        assert bad_result["status"] == STATUS_FAILED
        assert "briefDeliveryTime" in bad_result["reason"]



# --- helper + configuration edge cases ---------------------------------------


class TestOrderingGuardHelpers:
    """Direct coverage of the small pure helpers and config resolution."""

    def test_load_zoneinfo_valid(self) -> None:
        """A valid IANA name resolves to that zone."""
        tz = rbh._load_zoneinfo("America/Chicago")
        assert tz.key == "America/Chicago"

    def test_load_zoneinfo_unknown_defaults_to_utc(self, caplog) -> None:
        """An unknown timezone falls back to UTC with a warning."""
        with caplog.at_level(logging.WARNING):
            tz = rbh._load_zoneinfo("Not/AZone")
        assert tz.key == "UTC"

    def test_delivery_datetime_on_builds_local_datetime(self) -> None:
        """A valid HH:MM produces the expected local delivery datetime."""
        from datetime import date

        dt = rbh._delivery_datetime_on(date(2026, 8, 17), "06:30", CHICAGO)
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 17, 6, 30)

    def test_delivery_datetime_on_rejects_out_of_range(self) -> None:
        """'24:00' is rejected (hour out of range) with a clear ValueError."""
        from datetime import date

        with pytest.raises(ValueError):
            rbh._delivery_datetime_on(date(2026, 8, 17), "24:00", CHICAGO)

    def test_delivery_datetime_on_rejects_missing_colon(self) -> None:
        """A time with no colon is rejected."""
        from datetime import date

        with pytest.raises(ValueError):
            rbh._delivery_datetime_on(date(2026, 8, 17), "0630", CHICAGO)

    def test_next_brief_cron_fire_rolls_to_tomorrow_when_past(self) -> None:
        """When today's cron already fired, the next fire is tomorrow's."""
        completion = datetime(2026, 8, 17, 7, 0, tzinfo=CHICAGO)
        nxt = rbh.next_brief_cron_fire(completion, "06:30", "America/Chicago")
        assert nxt.date().isoformat() == "2026-08-18"
        assert (nxt.hour, nxt.minute) == (6, 30)

    def test_resolve_orchestrator_prefers_function_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The function-name env var is preferred over the ARN fallback."""
        monkeypatch.setenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", "stayos-orchestrator")
        monkeypatch.setenv("ORCHESTRATOR_ARN", "arn:aws:lambda:...:fn")
        assert rbh.resolve_lumi_orchestrator_function() == "stayos-orchestrator"

    def test_resolve_orchestrator_falls_back_to_arn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no function name, the ARN env var is used."""
        monkeypatch.delenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", raising=False)
        monkeypatch.setenv("ORCHESTRATOR_ARN", "arn:aws:lambda:us-east-1:0:function:x")
        assert rbh.resolve_lumi_orchestrator_function().endswith(":function:x")

    def test_resolve_orchestrator_raises_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With neither var set, a config error is raised."""
        monkeypatch.delenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_ARN", raising=False)
        with pytest.raises(rbh.BriefRegenerationConfigError):
            rbh.resolve_lumi_orchestrator_function()


class TestRegenerateBriefsNotConfigured:
    """When the orchestrator is not configured, briefs are skipped, not crashed."""

    def test_not_configured_marks_all_targets_failed_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing orchestrator config records every target as failed.

        The step must NOT raise (so written operational data is not rolled back,
        Requirement 4.3) and must surface the failure (Requirement 9.3).
        """
        monkeypatch.delenv("LUMI_ORCHESTRATOR_FUNCTION_NAME", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_ARN", raising=False)

        step_input = rbh.parse_step_input(dict(ROLL_FORWARD_EVENT))
        details = rbh.regenerate_briefs(step_input, pilot_property_ids=[])

        assert details["notConfigured"] is True
        assert details["briefsRequested"] == 1
        assert details["briefsFailed"] == 1
        assert details["briefsSucceeded"] == 0
        assert details["failedProperties"] == ["ALOHA-CHI-001"]
        assert details["results"][0]["reason"] == "not-configured"


class TestReadPropertyScheduleErrors:
    """The settings read degrades gracefully on a DynamoDB error."""

    def test_client_error_returns_none(self, brief_env, monkeypatch) -> None:
        """A ClientError during the settings scan yields None, not a crash."""
        from botocore.exceptions import ClientError

        class _ErroringTable:
            def scan(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                    "Scan",
                )

        monkeypatch.setattr(rbh, "_get_settings_table", lambda: _ErroringTable())
        assert rbh._read_property_schedule("ALOHA-CHI-001") is None

    def test_multiple_matches_picks_lowest_gm_alias(
        self, brief_env, monkeypatch, caplog
    ) -> None:
        """When several settings rows match, the lowest gmAlias is chosen (CR-3)."""

        class _MultiMatchTable:
            def scan(self, **kwargs):
                return {
                    "Items": [
                        {"gmAlias": "zzz", "propertyId": "ALOHA-CHI-001"},
                        {"gmAlias": "aaa", "propertyId": "ALOHA-CHI-001"},
                    ]
                }

        monkeypatch.setattr(rbh, "_get_settings_table", lambda: _MultiMatchTable())
        with caplog.at_level(logging.WARNING):
            schedule = rbh._read_property_schedule("ALOHA-CHI-001")
        assert schedule is not None
        assert schedule.gm_alias == "aaa"
