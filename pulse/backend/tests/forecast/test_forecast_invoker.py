"""Example tests for the PULSE Forecaster invoker Lambda (``pulse-forecaster-invoker``).

These integration-style example tests exercise the scheduling -> invocation hop
at ``functions/forecast-invoker/lambda_function.py`` (design Testing Strategy ->
Integration tests). They assert Requirement 1.4: a single ``{propertyId}``
Scheduler event triggers exactly one ``bedrock-agentcore:InvokeAgentRuntime``
start-session call carrying that ``propertyId`` as the session payload.

The invoker lives under ``functions/forecast-invoker`` (a hyphenated directory,
so not an importable package on the pytest path). It is therefore loaded by file
path via ``importlib.util.spec_from_file_location``. The module builds its
``bedrock-agentcore`` client at import time as a module-level attribute
(``_agentcore_client``); the tests replace that attribute with a recording fake
after import so no network connection is ever opened and no real boto3 client is
constructed. ``FORECAST_AGENT_RUNTIME_ARN`` is set before each handler call since
the handler reads it from the environment.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import pytest
from botocore.exceptions import ClientError

# Path to the invoker module, resolved relative to this test file:
# tests/forecast/ -> backend/ -> functions/forecast-invoker/lambda_function.py.
_INVOKER_PATH = (
    Path(__file__).resolve().parents[2]
    / "functions"
    / "forecast-invoker"
    / "lambda_function.py"
)

# A representative AgentCore Runtime ARN used as the FORECAST_AGENT_RUNTIME_ARN
# test value (never a real deployed ARN).
_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/pulse-forecaster-abc"
)

# A valid Scheduler target input: a single property identifier.
_PROPERTY_ID = "ALOHA-CHI-001"


class _FakeLambdaContext:
    """A minimal Lambda context for the Powertools inject_lambda_context decorator.

    The handler is wrapped with ``@logger.inject_lambda_context``, which reads
    these attributes off the context to enrich log records. A real Scheduler
    invocation supplies them; the tests supply this stand-in instead of ``None``.
    """

    function_name = "pulse-forecaster-invoker"
    memory_limit_in_mb = 128
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:pulse-forecaster-invoker"
    )
    aws_request_id = "test-request-id-0001"


class _FakeAgentCoreClient:
    """A fake ``bedrock-agentcore`` client recording invoke_agent_runtime calls.

    Attributes:
        calls: The recorded InvokeAgentRuntime keyword-argument dicts, in order.
    """

    def __init__(self, error: Optional[Exception] = None) -> None:
        """Initialize the fake.

        Args:
            error: Optional exception to raise on invocation, to simulate an
                InvokeAgentRuntime/API failure.
        """
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call and optionally raise the configured error.

        Args:
            **kwargs: The InvokeAgentRuntime keyword arguments the handler sends.

        Returns:
            A minimal stand-in response (the handler does not read it).

        Raises:
            Exception: The configured error, if any.
        """
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        # Minimal response mirroring the real dict-with-streaming-body shape; the
        # handler never reads it, so an object placeholder is sufficient.
        return {"statusCode": 200, "response": object()}


def _load_invoker_module() -> ModuleType:
    """Load the forecast-invoker ``lambda_function`` module by file path.

    The invoker directory is hyphenated and thus not importable as a package, so
    the module is loaded directly from its source file.

    Returns:
        The loaded ``lambda_function`` module object.
    """
    spec = importlib.util.spec_from_file_location(
        "forecast_invoker_lambda_function", _INVOKER_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load invoker module at {_INVOKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def invoker_module() -> ModuleType:
    """Provide the freshly loaded invoker module for each test."""
    return _load_invoker_module()


def test_event_triggers_exactly_one_start_session(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A {propertyId} event triggers exactly one start-session call (Req 1.4).

    Also verifies the call carries the FORECAST_AGENT_RUNTIME_ARN runtime ARN,
    the DEFAULT qualifier, a >= 33-character session id, and a JSON payload whose
    propertyId matches the event, and that the handler returns a summary dict.
    """
    # Runtime ARN comes from the environment (PYQUALITY-06 / NAMING-03).
    monkeypatch.setenv(
        invoker_module.ENV_FORECAST_AGENT_RUNTIME_ARN, _RUNTIME_ARN
    )
    fake_client = _FakeAgentCoreClient()
    # Replace the module-level client so no real boto3 client / network is used.
    monkeypatch.setattr(invoker_module, "_agentcore_client", fake_client)

    result = invoker_module.lambda_handler(
        {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
    )

    # Requirement 1.4: exactly one start-session call for the event.
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]

    # The call targets the configured runtime ARN at the DEFAULT endpoint.
    assert call["agentRuntimeArn"] == _RUNTIME_ARN
    assert call["qualifier"] == "DEFAULT"
    # InvokeAgentRuntime requires a session id of at least 33 characters.
    assert len(call["runtimeSessionId"]) >= 33

    # The payload is JSON bytes carrying the event's propertyId.
    payload = json.loads(call["payload"].decode("utf-8"))
    assert payload["propertyId"] == _PROPERTY_ID

    # The handler returns a summary dict without raising.
    assert result["invoked"] is True
    assert result["propertyId"] == _PROPERTY_ID
    assert result["runtimeSessionId"] == call["runtimeSessionId"]


def test_client_error_is_reraised_for_scheduler_retry(
    invoker_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ClientError from InvokeAgentRuntime propagates so the Scheduler retries (Req 1.6)."""
    monkeypatch.setenv(
        invoker_module.ENV_FORECAST_AGENT_RUNTIME_ARN, _RUNTIME_ARN
    )
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeAgentRuntime",
    )
    fake_client = _FakeAgentCoreClient(error=error)
    monkeypatch.setattr(invoker_module, "_agentcore_client", fake_client)

    # The handler logs and re-raises rather than swallowing the failure.
    with pytest.raises(ClientError):
        invoker_module.lambda_handler(
            {"propertyId": _PROPERTY_ID}, _FakeLambdaContext()
        )

    # It still attempted exactly one start-session before failing.
    assert len(fake_client.calls) == 1
