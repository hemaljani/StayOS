"""Shared fixtures and fakes for the Forecasting Agent AgentCore service tests.

The service modules live under ``backend/services/forecast-agent`` (a thin
``app`` entrypoint plus the importable ``forecast`` domain package), mirroring
the Triage Agent, rather than inside the ``pulse`` package. This conftest puts
that service directory on ``sys.path`` so the tests can ``import forecast.engine``
/ ``forecast.signals`` / ``import app`` directly.

The pure Forecast Engine (``forecast.engine``) needs no fakes - it has no I/O,
no clients, and no model, so it is unit/property tested directly. Every external
boundary of the seam-backed modules (the Gateway MCP tool caller, the narrative
model invoker, DynamoDB, and the realtime publisher) is faked here, so the tests
never open a network connection, construct a Strands Agent, call Bedrock, or
touch DynamoDB. ``strands`` / ``mcp_proxy_for_aws`` are not required to run these
tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Put the flat forecast-agent service modules on sys.path so `import forecast`
# and `import app` resolve. Inserted at the front so the service's modules win
# over any similarly-named top-level modules during the test session.
_SERVICE_DIR = (
    Path(__file__).resolve().parents[2] / "services" / "forecast-agent"
)
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))


@pytest.fixture(autouse=True)
def _forecast_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the model id / region / table / thresholds so the code resolves."""
    monkeypatch.setenv("FORECAST_MODEL_ID", "anthropic.claude-sonnet-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ALERTS_TABLE_NAME", "pulse-alerts")
    monkeypatch.setenv("GATEWAY_ENDPOINT_URL", "https://gateway.example/mcp")
    monkeypatch.setenv(
        "FORECAST_ENABLED_PROPERTY_IDS",
        "ALOHA-CHI-001,ALOHA-MIA-001,ALOHA-TYO-001,ALOHA-MAD-001,ALOHA-BOM-001",
    )
    monkeypatch.setenv("FORECAST_HORIZON_DAYS", "14")
    monkeypatch.setenv("FORECAST_CONFIDENCE_THRESHOLD", "50")
    monkeypatch.setenv("FORECAST_MIN_DATA_POINTS", "3")


class RecordingToolCaller:
    """A fake Gateway tool caller that records calls and returns canned results.

    Attributes:
        calls: The recorded ``(tool_name, arguments)`` tuples, in call order.
        results: Mapping of tool name -> canned JSON result to return.
    """

    def __init__(self, results: dict[str, Any]) -> None:
        """Initialize with a mapping of tool name -> canned result."""
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Record the call and return the canned result for ``tool_name``."""
        self.calls.append((tool_name, dict(arguments)))
        if tool_name not in self.results:
            raise AssertionError(f"unexpected tool call: {tool_name}")
        return self.results[tool_name]

    def property_ids(self) -> list[Any]:
        """Return the ``propertyId`` argument passed to every recorded call."""
        return [args.get("propertyId") for _name, args in self.calls]

    def tool_names(self) -> list[str]:
        """Return the tool name of every recorded call, in call order."""
        return [name for name, _args in self.calls]


class _ConditionalCheckFailed(Exception):
    """Stand-in for botocore's ConditionalCheckFailedException."""


class _FakeClient:
    """Fake DynamoDB client exposing the exceptions namespace write checks."""

    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions shape
        ConditionalCheckFailedException = _ConditionalCheckFailed


class _FakeMeta:
    """Fake ``table.meta`` exposing ``.client.exceptions``."""

    client = _FakeClient()


class FakeAlertsTable:
    """In-memory fake of a ``pulse-alerts`` DynamoDB table resource.

    Attributes:
        item: The stored alert item returned by ``get_item`` (or ``None``).
        query_items: Items returned by ``query`` (the open-record lookup).
        puts: Recorded ``put_item`` keyword calls.
        updates: Recorded ``update_item`` keyword calls.
        raise_conditional: When ``True``, the mutating call raises the
            conditional check failure (simulating a race with a resolve).
    """

    def __init__(
        self,
        item: dict[str, Any] | None = None,
        *,
        query_items: list[dict[str, Any]] | None = None,
        raise_conditional: bool = False,
    ) -> None:
        """Initialize the fake table."""
        self.item = item
        self.query_items = query_items or []
        self.puts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.raise_conditional = raise_conditional
        self.meta = _FakeMeta()

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803 - boto3 API
        """Return ``{"Item": item}`` (or ``{}`` when there is no item)."""
        return {"Item": self.item} if self.item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Return the canned open-record query result."""
        return {"Items": list(self.query_items)}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Record the put, or raise the conditional check failure."""
        if self.raise_conditional:
            raise _ConditionalCheckFailed()
        self.puts.append(kwargs)
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Record the update, or raise the conditional check failure."""
        if self.raise_conditional:
            raise _ConditionalCheckFailed()
        self.updates.append(kwargs)
        return {}


class RecordingPublisher:
    """A fake realtime publisher seam recording published channels and events."""

    def __init__(self, *, fail: bool = False) -> None:
        """Initialize with an empty publish log (optionally always failing)."""
        self.published: list[tuple[str, list[Any]]] = []
        self.fail = fail

    def __call__(self, channel: str, events: Any) -> None:
        """Record a publish call, or raise when configured to fail."""
        if self.fail:
            raise RuntimeError("realtime publish failed (test)")
        self.published.append((channel, list(events)))


def make_json_invoker(raw_json: str) -> Any:
    """Build a fake narrative model invoker returning fixed text.

    Ignores the model id and prompt, mirroring the Triage Agent test helper. Use
    to inject a present, well-behaved model that authors a Remediation_Plan.
    """

    def _invoke(_model_id: str, _prompt: str) -> str:
        return raw_json

    return _invoke


def make_failing_invoker() -> Any:
    """Build a fake model invoker that always raises (exercises the fallback)."""

    def _invoke(_model_id: str, _prompt: str) -> str:
        raise RuntimeError("model unavailable (test)")

    return _invoke


def make_step_clock(values: list[float]) -> Any:
    """Build a wall clock that returns successive ``values`` on each call.

    Used to drive deterministic retry/backoff timing assertions in the Signal
    Reader tests.
    """
    state = {"i": 0}

    def _clock() -> float:
        i = state["i"]
        state["i"] = min(i + 1, len(values) - 1)
        return values[i]

    return _clock
