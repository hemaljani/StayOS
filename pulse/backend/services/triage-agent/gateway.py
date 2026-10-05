"""Shared StayOS AgentCore Gateway MCP connection for the Triage Agent.

The Triage Agent gathers live hotel-operations facts by calling read-only tools
over the shared StayOS AgentCore Gateway via MCP, exactly like the LUMI chat
agent (``lumi/backend/services/chat-agent/server.py``): the Gateway uses AWS_IAM
inbound auth, so tool discovery and invocation are SigV4-signed via
``aws_iam_streamablehttp_client`` and wrapped in a Strands ``MCPClient``. The
endpoint comes from SSM ``/${StackPrefix}/gateway/endpoint-url`` (injected as
``GATEWAY_ENDPOINT_URL``); the runtime IAM role grants
``bedrock-agentcore:InvokeGateway``.

This module exposes:
    * :class:`GatewayToolClient` -- an open MCP connection used as a context
      manager, exposing :meth:`discover_tools` (mirrors the chat agent's
      ``list_tools_sync`` so a Strands Agent can be built with the same tools)
      and :meth:`call_tool`, which invokes one Gateway tool and returns its
      parsed JSON result.
    * :data:`ToolCaller` -- the ``Callable[[str, dict], Any]`` seam the situation
      builders depend on, so they are unit-testable with an in-memory fake and
      never open a network connection.

Heavy third-party imports (``mcp_proxy_for_aws``, ``strands``) are performed
lazily inside :meth:`GatewayToolClient.connect` so importing this module (and
the situation builders that reference the :data:`ToolCaller` type) never
requires those packages to be installed -- the unit tests inject a fake caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional

from pulse.common.errors import TriageFailure
from pulse.common.logging import get_logger

logger = get_logger("pulse-triage-agent")

# The situation builders depend only on this seam: given a tool name and an
# arguments mapping (always including propertyId), return the tool's parsed JSON
# result. The real implementation is GatewayToolClient.call_tool; tests inject a
# fake that records calls and returns canned results.
ToolCaller = Callable[[str, dict[str, Any]], Any]


def _extract_tool_json(result: Any) -> Any:
    """Extract and JSON-decode the payload from an MCP tool-call result.

    Strands' ``MCPClient.call_tool_sync`` returns an MCP result whose ``content``
    is a list of content blocks; the Gateway Lambda returns its payload as a
    single text block of JSON. This helper reads that text defensively (handling
    both dict-shaped and object-shaped results across SDK versions) and decodes
    it into native Python.

    Args:
        result: The raw MCP tool-call result.

    Returns:
        The decoded JSON payload (dict or list), or the raw text when the text
        is not valid JSON (so a plain-string tool result is still usable).

    Raises:
        TriageFailure: If the result carries no readable text content, or the
            tool reported an error.
    """
    # Content may be attribute-style (result.content) or dict-style
    # (result["content"]); normalize to a list of blocks.
    content = None
    if isinstance(result, dict):
        content = result.get("content")
        if result.get("isError"):
            raise TriageFailure(
                f"Gateway tool reported an error: {content!r}",
                reason="gateway_tool_error",
            )
    else:
        content = getattr(result, "content", None)
        if getattr(result, "isError", False):
            raise TriageFailure(
                f"Gateway tool reported an error: {content!r}",
                reason="gateway_tool_error",
            )

    if not content:
        raise TriageFailure(
            "Gateway tool result had no content", reason="gateway_tool_error"
        )

    # Find the first text block. Blocks may be dict-style ({"type","text"}) or
    # object-style (block.text).
    text: Optional[str] = None
    for block in content:
        if isinstance(block, dict):
            candidate = block.get("text")
        else:
            candidate = getattr(block, "text", None)
        if isinstance(candidate, str) and candidate:
            text = candidate
            break

    if text is None:
        raise TriageFailure(
            "Gateway tool result had no text content", reason="gateway_tool_error"
        )

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # A non-JSON string result is returned as-is; callers that expect JSON
        # will handle the unexpected shape defensively.
        return text


class GatewayToolClient:
    """An open MCP connection to the shared StayOS Gateway (context manager).

    Mirrors the LUMI chat agent's connection: an ``aws_iam_streamablehttp_client``
    transport (SigV4, service ``bedrock-agentcore``) wrapped in a Strands
    ``MCPClient`` whose context stays open for the duration of one triage
    invocation. Use as::

        with GatewayToolClient(endpoint, region) as gateway:
            tools = gateway.discover_tools()          # for the Strands Agent
            occ = gateway.call_tool("get_occupancy", {"propertyId": pid})

    Attributes:
        endpoint: The Gateway MCP endpoint URL.
        region: The AWS region for the SigV4 signer.
        aws_service: The signing service name (``bedrock-agentcore``).
    """

    def __init__(self, endpoint: str, region: str, aws_service: str) -> None:
        """Initialize the Gateway client (does not open the connection yet).

        Args:
            endpoint: The Gateway MCP endpoint URL (from ``GATEWAY_ENDPOINT_URL``).
            region: The AWS region for the Gateway SigV4 signer.
            aws_service: The signing service name (``bedrock-agentcore``).
        """
        self.endpoint = endpoint
        self.region = region
        self.aws_service = aws_service
        self._mcp_client: Any = None
        # Map of bare tool name (e.g. "get_room_status") -> the actual name the
        # Gateway exposes over MCP. AgentCore Gateway namespaces a Lambda
        # target's tools as "<targetName>___<toolName>" (e.g.
        # "tools___get_room_status"), so a direct call by the BARE name is
        # rejected as "Unknown tool" (BUG-028). Populated from discover_tools().
        self._tool_name_map: dict[str, str] = {}

    def connect(self) -> None:
        """Open the Gateway MCP connection (SigV4/IAM, Streamable HTTP).

        Imports the transport and MCP client lazily so this module imports
        cleanly in environments where ``mcp_proxy_for_aws`` / ``strands`` are not
        installed (the unit tests never open a real connection).

        Raises:
            TriageFailure: If the connection cannot be established.
        """
        # Lazy imports: keep module import side-effect free for unit tests.
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
        from strands.tools.mcp.mcp_client import MCPClient

        try:
            self._mcp_client = MCPClient(
                lambda: aws_iam_streamablehttp_client(
                    endpoint=self.endpoint,
                    aws_region=self.region,
                    aws_service=self.aws_service,
                )
            )
            # Enter the MCP context manually so the connection stays open across
            # the multiple tool calls one triage invocation makes (mirrors the
            # chat agent, which enters __enter__ for the whole session).
            self._mcp_client.__enter__()
        except Exception as error:  # noqa: BLE001 - any connect error -> triage failure
            raise TriageFailure(
                f"Failed to connect to the shared Gateway: {error}",
                reason="gateway_connect_error",
            ) from error

    def discover_tools(self) -> list[Any]:
        """List the tools registered on the Gateway (``tools/list`` via MCP).

        Also records a bare-name -> actual-name map so :meth:`call_tool` can be
        called with a bare tool name (e.g. ``get_room_status``) even though the
        Gateway exposes it namespaced (e.g. ``tools___get_room_status``).

        Returns:
            The discovered tool objects (used to build the Strands Agent with the
            same tool set the chat agent uses).

        Raises:
            TriageFailure: If the connection is not open.
        """
        if self._mcp_client is None:
            raise TriageFailure(
                "Gateway connection is not open", reason="gateway_connect_error"
            )
        tools = self._mcp_client.list_tools_sync()
        # Build the bare -> actual name map. Discovered tools may expose their
        # name as ``.tool_name`` (Strands) or ``.name`` (raw MCP); handle both.
        self._tool_name_map = {}
        for tool in tools:
            actual = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
            if not isinstance(actual, str) or not actual:
                continue
            # The bare name is the segment after the "<target>___" prefix.
            bare = actual.rsplit("___", 1)[-1]
            self._tool_name_map[bare] = actual
            self._tool_name_map[actual] = actual
        return tools

    def _resolve_tool_name(self, tool_name: str) -> str:
        """Resolve a (possibly bare) tool name to the Gateway's actual name.

        Args:
            tool_name: The tool name the caller passed (typically bare, e.g.
                ``get_room_status``).

        Returns:
            The namespaced name the Gateway exposes when known (e.g.
            ``tools___get_room_status``), else the input unchanged (so behavior
            is unchanged when discovery has not run or the Gateway does not
            namespace).
        """
        return self._tool_name_map.get(tool_name, tool_name)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one Gateway tool and return its parsed JSON result.

        Args:
            tool_name: The Gateway tool name (e.g. ``get_walkable_guests``).
            arguments: The tool arguments; MUST include ``propertyId`` so the
                Gateway scopes the result server-side.

        Returns:
            The tool's decoded JSON result.

        Raises:
            TriageFailure: If the connection is not open or the tool call fails.
        """
        if self._mcp_client is None:
            raise TriageFailure(
                "Gateway connection is not open", reason="gateway_connect_error"
            )
        # Resolve a bare name (e.g. "get_room_status") to the Gateway's actual
        # namespaced name (e.g. "tools___get_room_status"); see BUG-028.
        resolved_name = self._resolve_tool_name(tool_name)
        logger.info(
            "Triage agent invoking Gateway tool",
            extra={
                "tool_name": tool_name,
                "resolved_tool_name": resolved_name,
                "property_id": arguments.get("propertyId"),
            },
        )
        try:
            result = self._mcp_client.call_tool_sync(
                tool_use_id=f"triage-{tool_name}",
                name=resolved_name,
                arguments=arguments,
            )
        except TriageFailure:
            raise
        except Exception as error:  # noqa: BLE001 - any tool error -> triage failure
            raise TriageFailure(
                f"Gateway tool {tool_name!r} invocation failed: {error}",
                reason="gateway_tool_error",
            ) from error
        return _extract_tool_json(result)

    def close(self) -> None:
        """Close the Gateway MCP connection (best-effort)."""
        if self._mcp_client is not None:
            try:
                self._mcp_client.__exit__(None, None, None)
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "Error closing Gateway MCP connection",
                    extra={"error": str(error)},
                )
            self._mcp_client = None

    def __enter__(self) -> GatewayToolClient:
        """Open the connection and return self for use in a ``with`` block."""
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        """Close the connection on ``with`` block exit."""
        self.close()


__all__ = ["ToolCaller", "GatewayToolClient", "_extract_tool_json"]
