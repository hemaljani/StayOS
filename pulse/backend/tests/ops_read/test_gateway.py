"""Unit tests for the ops-read facade Gateway client tool-name resolution.

BUG-031 (the ops-read twin of triage BUG-028): AgentCore Gateway namespaces a
Lambda target's tools as ``<targetName>___<toolName>`` (e.g.
``tools___get_vip_guests``). The ``pulse-ops-read`` facade called tools by their
BARE name (``get_vip_guests``, ``get_occupancy``, ...), which the MCP server
rejected, so every VIPs/Ops tool call failed and the facade degraded to empty
data (VIPs count 0, empty Ops). ``call_tool`` now resolves a bare name to the
Gateway's namespaced name, discovering the tool list lazily on first use.

These tests inject a fake MCP client so no network connection, Strands, or
``mcp_proxy_for_aws`` import is needed.

# Feature: initial-pulse-project - ops-read Gateway tool-name resolution (BUG-031)
"""

from __future__ import annotations

from typing import Any

from pulse.ops_read.gateway import GatewayToolClient


class _FakeTool:
    """A discovered MCP tool exposing a namespaced ``tool_name`` (Strands shape)."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name


class _FakeMcpClient:
    """Records call_tool_sync invocations and returns a canned text result."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self.calls: list[dict[str, Any]] = []
        self.list_calls = 0

    def list_tools_sync(self) -> list[Any]:
        self.list_calls += 1
        return [_FakeTool(name) for name in self._tool_names]

    def call_tool_sync(self, *, tool_use_id: str, name: str, arguments: dict) -> Any:
        self.calls.append({"name": name, "arguments": arguments})
        # Minimal MCP result shape _extract_tool_json understands.
        return {"content": [{"type": "text", "text": '{"status":"success","data":{}}'}]}


def _client_with(tool_names: list[str]) -> tuple[GatewayToolClient, _FakeMcpClient]:
    """Build a GatewayToolClient wired to a fake MCP client with given tools."""
    client = GatewayToolClient(
        "https://gw.example/mcp", "us-east-1", "bedrock-agentcore"
    )
    fake = _FakeMcpClient(tool_names)
    client._mcp_client = fake  # inject the open connection
    return client, fake


def test_bare_name_resolves_to_namespaced_via_lazy_discovery() -> None:
    """A bare tool name is sent to the Gateway as its namespaced name.

    Discovery is triggered lazily by call_tool (the facade never calls
    discover_tools explicitly).
    """
    client, fake = _client_with(
        ["tools___get_vip_guests", "tools___get_occupancy"]
    )

    client.call_tool("get_vip_guests", {"propertyId": "P-1"})

    assert fake.list_calls == 1  # lazily discovered
    assert fake.calls[0]["name"] == "tools___get_vip_guests"


def test_already_namespaced_name_is_passed_through() -> None:
    """Passing the full namespaced name still resolves to itself."""
    client, fake = _client_with(["tools___get_occupancy"])

    client.call_tool("tools___get_occupancy", {"propertyId": "P-1"})

    assert fake.calls[0]["name"] == "tools___get_occupancy"


def test_unnamespaced_gateway_name_is_unchanged() -> None:
    """Against a non-namespacing Gateway, the bare name is used unchanged."""
    client, fake = _client_with(["get_occupancy"])  # not namespaced

    client.call_tool("get_occupancy", {"propertyId": "P-1"})

    assert fake.calls[0]["name"] == "get_occupancy"


def test_discovery_runs_once_across_multiple_calls() -> None:
    """The name map is cached: discovery happens once, not per tool call."""
    client, fake = _client_with(
        ["tools___get_vip_guests", "tools___get_room_status"]
    )

    client.call_tool("get_vip_guests", {"propertyId": "P-1"})
    client.call_tool("get_room_status", {"propertyId": "P-1"})

    assert fake.list_calls == 1
    assert fake.calls[0]["name"] == "tools___get_vip_guests"
    assert fake.calls[1]["name"] == "tools___get_room_status"
