"""Unit tests for the Triage Agent Gateway client tool-name resolution.

BUG-028: AgentCore Gateway namespaces a Lambda target's tools as
``<targetName>___<toolName>`` (e.g. ``tools___get_room_status``). The triage
agent's deterministic fact-gathering calls tools by their BARE name
(``get_room_status``), which the MCP server rejected as "Unknown tool", so
VIP Room / OOO Cluster triage failed and no brief attached. ``discover_tools``
now records a bare -> actual name map and ``call_tool`` resolves through it.

These tests inject a fake MCP client so no network connection, Strands Agent, or
``mcp_proxy_for_aws`` import is needed.
"""

from __future__ import annotations

from typing import Any

from gateway import GatewayToolClient


class _FakeTool:
    """A discovered MCP tool exposing a namespaced ``tool_name`` (Strands shape)."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name


class _FakeMcpClient:
    """Records call_tool_sync invocations and returns a canned text result."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self.calls: list[dict[str, Any]] = []

    def list_tools_sync(self) -> list[Any]:
        return [_FakeTool(name) for name in self._tool_names]

    def call_tool_sync(self, *, tool_use_id: str, name: str, arguments: dict) -> Any:
        self.calls.append({"name": name, "arguments": arguments})
        # Minimal MCP result shape _extract_tool_json understands.
        return {"content": [{"type": "text", "text": '{"status":"ok","data":{}}'}]}


def _client_with(tool_names: list[str]) -> tuple[GatewayToolClient, _FakeMcpClient]:
    """Build a GatewayToolClient wired to a fake MCP client with given tools."""
    client = GatewayToolClient(
        "https://gw.example/mcp", "us-east-1", "bedrock-agentcore"
    )
    fake = _FakeMcpClient(tool_names)
    client._mcp_client = fake  # inject the open connection
    return client, fake


def test_bare_name_resolves_to_namespaced_after_discovery() -> None:
    """A bare tool name is sent to the Gateway as its namespaced name."""
    client, fake = _client_with(
        ["tools___get_room_status", "tools___get_room_move_candidates"]
    )
    client.discover_tools()

    client.call_tool("get_room_status", {"propertyId": "P-1"})

    assert fake.calls[0]["name"] == "tools___get_room_status"


def test_already_namespaced_name_is_passed_through() -> None:
    """Passing the full namespaced name still resolves to itself."""
    client, fake = _client_with(["tools___get_occupancy"])
    client.discover_tools()

    client.call_tool("tools___get_occupancy", {"propertyId": "P-1"})

    assert fake.calls[0]["name"] == "tools___get_occupancy"


def test_unknown_or_unnamespaced_name_is_unchanged() -> None:
    """Without a mapping (e.g. a non-namespacing Gateway), the name is unchanged."""
    client, fake = _client_with(["get_occupancy"])  # not namespaced
    client.discover_tools()

    client.call_tool("get_occupancy", {"propertyId": "P-1"})

    assert fake.calls[0]["name"] == "get_occupancy"
