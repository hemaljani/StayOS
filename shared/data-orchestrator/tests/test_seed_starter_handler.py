"""Unit tests for the first-deploy seed-starter custom-resource handler.

Verify that Create/Update start a seed-mode execution, Delete is a no-op that
never touches data (Requirement 8.1), the state-machine ARN comes from the
environment (PYQUALITY-06), and CloudFormation is always signalled. External
boundaries (Step Functions, the CFN response URL) are mocked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import seed_starter_handler
from orchestrator_common import MODE_SEED

STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:stayos-data-orchestrator"
FAKE_CONTEXT = SimpleNamespace(
    function_name="stayos-data-seed-starter",
    memory_limit_in_mb=256,
    invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:stayos-data-seed-starter",
    aws_request_id="test-request-id",
    log_stream_name="2026/08/17/[$LATEST]abc123",
)


@pytest.fixture()
def captured_cfn(monkeypatch):
    """Capture CFN signal calls instead of making an HTTP PUT."""
    captured: dict = {}

    def _fake_send(event, context, status, data=None, reason=None):
        captured["status"] = status
        captured["data"] = data
        captured["reason"] = reason

    monkeypatch.setattr(seed_starter_handler, "_send_cfn_response", _fake_send)
    return captured


@pytest.fixture()
def fake_sfn(monkeypatch):
    """Replace the module-level Step Functions client with a recorder."""
    recorder: dict = {}

    def _fake_start_execution(*, stateMachineArn, name, input):  # noqa: N803 - boto3 kwargs
        recorder["stateMachineArn"] = stateMachineArn
        recorder["name"] = name
        recorder["input"] = json.loads(input)
        return {"executionArn": f"{stateMachineArn}:exec:{name}"}

    fake_client = SimpleNamespace(start_execution=_fake_start_execution)
    monkeypatch.setattr(seed_starter_handler, "_SFN_CLIENT", fake_client)
    monkeypatch.setenv(seed_starter_handler.ENV_STATE_MACHINE_ARN, STATE_MACHINE_ARN)
    return recorder


class TestCreate:
    def test_starts_seed_execution(self, fake_sfn, captured_cfn) -> None:
        event = {
            "RequestType": "Create",
            "ResponseURL": "https://cfn.example/response",
            "StackId": "stack",
            "RequestId": "req",
            "LogicalResourceId": "SeedCustomResource",
        }
        result = seed_starter_handler.lambda_handler(event, FAKE_CONTEXT)

        assert result["status"] == "started"
        assert fake_sfn["stateMachineArn"] == STATE_MACHINE_ARN
        # Seed mode fans out (no propertyId), anchored to today by the steps.
        assert fake_sfn["input"]["mode"] == MODE_SEED
        assert fake_sfn["input"]["propertyId"] is None
        assert captured_cfn["status"] == "SUCCESS"


class TestUpdate:
    def test_update_also_starts_execution(self, fake_sfn, captured_cfn) -> None:
        event = {"RequestType": "Update"}
        result = seed_starter_handler.lambda_handler(event, FAKE_CONTEXT)
        assert result["status"] == "started"
        assert fake_sfn["input"]["mode"] == MODE_SEED
        assert captured_cfn["status"] == "SUCCESS"


class TestDelete:
    def test_delete_is_a_noop(self, fake_sfn, captured_cfn) -> None:
        event = {"RequestType": "Delete"}
        result = seed_starter_handler.lambda_handler(event, FAKE_CONTEXT)
        # No execution started - deleting the stack must never touch data.
        assert result["status"] == "skipped"
        assert "stateMachineArn" not in fake_sfn
        assert captured_cfn["status"] == "SUCCESS"


class TestFailureSignalling:
    def test_missing_arn_reports_failed(self, monkeypatch, captured_cfn) -> None:
        # No STATE_MACHINE_ARN in the environment -> KeyError -> FAILED signal.
        monkeypatch.delenv(seed_starter_handler.ENV_STATE_MACHINE_ARN, raising=False)
        event = {"RequestType": "Create"}
        with pytest.raises(KeyError):
            seed_starter_handler.lambda_handler(event, FAKE_CONTEXT)
        assert captured_cfn["status"] == "FAILED"
