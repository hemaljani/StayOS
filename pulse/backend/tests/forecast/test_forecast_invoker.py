"""Forecast invocation tests with real SDK response streams and no AWS calls."""

from __future__ import annotations

import importlib
import io
import json
from datetime import datetime
from types import ModuleType
from typing import Any, Optional

import boto3
import pytest
from botocore.exceptions import ClientError, IncompleteReadError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
    "runtime/pulse_forecaster-Abc1234567"
)
_PROPERTY_ID = "ALOHA-CHI-001"


class _FakeLambdaContext:
    function_name = "pulse-forecaster-invoker"
    memory_limit_in_mb = 128
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:pulse-forecaster-invoker"
    )
    aws_request_id = "test-request-id-0001"


def _summary(**overrides: Any) -> dict[str, Any]:
    return {
        "propertyId": _PROPERTY_ID,
        "predictionsWritten": 2,
        "resolved": 1,
        "noAlert": False,
        **overrides,
    }


def _response(
    summary: Any, *, status: int = 200, content_type: str = "application/json"
) -> dict[str, Any]:
    raw = summary if isinstance(summary, bytes) else json.dumps(summary).encode()
    return {
        "statusCode": status,
        "contentType": content_type,
        "response": StreamingBody(io.BytesIO(raw), len(raw)),
    }


class _FakeAgentCoreClient:
    def __init__(
        self,
        error: Optional[Exception] = None,
        response: Optional[dict[str, Any]] = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self.response = response if response is not None else _response(_summary())

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self.response


@pytest.fixture()
def invoker_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("FORECAST_AGENT_RUNTIME_ARN", _RUNTIME_ARN)
    return importlib.reload(importlib.import_module("pulse.forecast_invoker"))


def test_event_invokes_once_and_returns_completed_forecast(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentCoreClient()
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    result = invoker_module.lambda_handler(
        {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["agentRuntimeArn"] == _RUNTIME_ARN
    assert call["qualifier"] == "DEFAULT"
    assert call["contentType"] == call["accept"] == "application/json"
    assert len(call["runtimeSessionId"]) >= 33
    assert json.loads(call["payload"]) == {"propertyId": _PROPERTY_ID}
    assert result["invoked"] is True
    assert result["completed"] is True
    assert result["runtimeSessionId"] == call["runtimeSessionId"]
    assert datetime.fromisoformat(result["completedAt"]).utcoffset() is not None
    for key, value in _summary().items():
        assert result[key] == value
    assert client.response["response"]._raw_stream.closed


def test_sdk_call_matches_agentcore_api_model(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = boto3.client(
        "bedrock-agentcore",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "invoke_agent_runtime",
            _response(_summary()),
            {
                "agentRuntimeArn": _RUNTIME_ARN,
                "runtimeSessionId": ANY,
                "qualifier": "DEFAULT",
                "contentType": "application/json",
                "accept": "application/json",
                "payload": json.dumps({"propertyId": _PROPERTY_ID}).encode(),
            },
        )
        result = invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
        assert result["completed"] is True
        stubber.assert_no_pending_responses()


def test_successful_no_alert_outcome_is_completed(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentCoreClient(
        response=_response(_summary(predictionsWritten=0, noAlert=True))
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    result = invoker_module.lambda_handler(
        {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
    )
    assert result["completed"] is True
    assert result["noAlert"] is True


def test_client_error_is_reraised(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeAgentRuntime",
    )
    client = _FakeAgentCoreClient(error=error)
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(ClientError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "summary",
    [
        {"propertyId": _PROPERTY_ID, "failed": True, "reason": "RuntimeError"},
        {"propertyId": None, "rejected": True, "reason": "property_disabled"},
        _summary(propertyId="ALOHA-MIA-001"),
        _summary(predictionsWritten=-1),
        _summary(predictionsWritten=True),
        _summary(resolved="1"),
        _summary(noAlert=True),
        _summary(noAlert="false"),
        {"propertyId": _PROPERTY_ID},
        [],
        b"invalid JSON",
        b"\xff",
        b" " * (64 * 1024 + 1),
    ],
)
def test_failed_or_invalid_results_raise_and_close_body(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    summary: Any,
) -> None:
    client = _FakeAgentCoreClient(response=_response(summary))
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(RuntimeError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
    assert client.response["response"]._raw_stream.closed


@pytest.mark.parametrize(
    "status,content_type", [(500, "application/json"), (200, "text/event-stream")]
)
def test_unsuccessful_transport_or_wrong_content_type_raises(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
) -> None:
    client = _FakeAgentCoreClient(
        response=_response(_summary(), status=status, content_type=content_type)
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(RuntimeError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
    assert client.response["response"]._raw_stream.closed


def test_missing_body_raises(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentCoreClient(
        response={"statusCode": 200, "contentType": "application/json"}
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(RuntimeError, match="no response body"):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )


def test_stream_read_error_raises_and_closes_body(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenBody(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise TimeoutError("stream stalled")

    body = BrokenBody()
    client = _FakeAgentCoreClient(
        response={
            "statusCode": 200,
            "contentType": "application/json",
            "response": body,
        }
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(TimeoutError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
    assert body.closed


def test_truncated_stream_cannot_report_completion(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(_summary()).encode()
    body = StreamingBody(io.BytesIO(raw), len(raw) + 5)
    client = _FakeAgentCoreClient(
        response={
            "statusCode": 200,
            "contentType": "application/json",
            "response": body,
        }
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(IncompleteReadError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )
    assert body._raw_stream.closed


def test_json_with_charset_is_accepted(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentCoreClient(
        response=_response(_summary(), content_type="application/json; charset=utf-8")
    )
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    result = invoker_module.lambda_handler(
        {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
    )
    assert result["completed"] is True


def test_client_is_created_lazily_and_reused(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    client = _FakeAgentCoreClient()
    factory = Mock(return_value=client)
    monkeypatch.setattr(invoker_module.boto3, "client", factory)
    assert invoker_module._agentcore_client is None
    invoker_module.lambda_handler({"propertyId": _PROPERTY_ID}, _FakeLambdaContext())
    client.response = _response(_summary())
    invoker_module.lambda_handler({"propertyId": _PROPERTY_ID}, _FakeLambdaContext())
    factory.assert_called_once_with(
        "bedrock-agentcore", config=invoker_module.BOTO_CONFIG
    )
    assert len(client.calls) == 2
    assert client.calls[0]["runtimeSessionId"] != client.calls[1]["runtimeSessionId"]


@pytest.mark.parametrize(
    "event",
    [{}, {"propertyId": ""}, {"propertyId": " "}, {"propertyId": 12}, None],
)
def test_invalid_property_does_not_invoke(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    event: Any,
) -> None:
    client = _FakeAgentCoreClient()
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    with pytest.raises(ValueError):
        invoker_module.lambda_handler(event, _FakeLambdaContext())
    assert not client.calls


@pytest.mark.parametrize("runtime_arn", ["", "   ", None])
def test_unconfigured_runtime_is_an_explicit_skip(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    runtime_arn: Optional[str],
) -> None:
    if runtime_arn is None:
        monkeypatch.delenv("FORECAST_AGENT_RUNTIME_ARN")
    else:
        monkeypatch.setenv("FORECAST_AGENT_RUNTIME_ARN", runtime_arn)
    client = _FakeAgentCoreClient()
    monkeypatch.setattr(invoker_module, "_agentcore_client", client)
    result = invoker_module.lambda_handler(
        {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
    )
    assert result == {
        "invoked": False,
        "completed": False,
        "propertyId": _PROPERTY_ID,
        "reason": "runtime_not_configured",
    }
    assert not client.calls
