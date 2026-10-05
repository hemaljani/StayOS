"""Shared StayOS AgentCore Gateway MCP client for the VIPs/Ops facade.

The ``pulse-ops-read`` facade gathers live hotel-operations facts by calling
read-only tools over the shared StayOS AgentCore Gateway via MCP, exactly like
the PULSE Triage Agent (``backend/services/triage-agent/gateway.py``) and the
LUMI chat agent: the Gateway uses AWS_IAM inbound auth, so tool discovery and
invocation are SigV4-signed via ``aws_iam_streamablehttp_client`` and wrapped in
a Strands ``MCPClient``. The endpoint comes from SSM
``/${StackPrefix}/gateway/endpoint-url`` (injected as ``GATEWAY_ENDPOINT_URL``);
the facade's IAM role grants ``bedrock-agentcore:InvokeGateway``.

This module intentionally mirrors the triage Gateway pattern (rather than
importing it) because the triage module lives under ``backend/services`` as a
flat container module, not inside the importable ``pulse`` package. Keeping a
faithful copy here lets the facade live in the shared package while preserving
the same battle-tested connect / extract / call semantics.

This module exposes:
    * :class:`GatewayToolClient` -- an open MCP connection (context manager)
      exposing :meth:`call_tool`, which invokes one Gateway tool and returns its
      parsed JSON result.
    * :data:`ToolCaller` -- the ``Callable[[str, dict], Any]`` seam the shaping
      functions depend on, so they are unit-testable with an in-memory fake and
      never open a network connection.
    * :func:`tool_data` -- unwraps the shared tool Lambda's ``{status, data}``
      envelope (and treats a non-``success`` status as a failure).

Heavy third-party imports (``mcp_proxy_for_aws``, ``strands``) are performed
lazily inside :meth:`GatewayToolClient.connect` so importing this module (and
the shaping functions that reference the :data:`ToolCaller` type) never requires
those packages to be installed -- the unit tests inject a fake caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional

from pulse.common.errors import OpsReadFailure
from pulse.common.logging import get_logger

logger = get_logger("pulse-ops-read")

# The signing service name for the shared StayOS AgentCore Gateway (SigV4).
GATEWAY_AWS_SERVICE = "bedrock-agentcore"

# The shaping functions depend only on this seam: given a tool name and an
# arguments mapping (always including propertyId), return the tool's parsed JSON
# result. The real implementation is GatewayToolClient.call_tool; tests inject a
# fake that records calls and returns canned results.
ToolCaller = Callable[[str, dict[str, Any]], Any]


def tool_data(result: Any, tool_name: str) -> dict[str, Any]:
    """Unwrap the shared tool Lambda's ``{status, data}`` response envelope.

    The shared StayOS tool Lambda (``lumi/backend/functions/tools``) returns
    ``{"status": "success", "data": {...}}`` on success and
    ``{"status": "unavailable", "message": ...}`` when data cannot be produced.
    This normalizes both: it returns the inner ``data`` mapping on success, and
    raises :class:`OpsReadFailure` when the tool reports a non-``success``
    status so the facade degrades to a clean error envelope.

    A bare mapping without a ``status`` key (as some fakes/tools may return) is
    passed through unchanged, so the shaping code tolerates both shapes.

    Args:
        result: The decoded tool result (from :meth:`GatewayToolClient.call_tool`
            or an injected fake).
        tool_name: The Gateway tool name, used for error context.

    Returns:
        The tool's data mapping (empty dict when the payload is not a mapping).

    Raises:
        OpsReadFailure: If the tool reported a non-``success`` status.
    """
    if not isinstance(result, dict):
        return {}
    status = result.get("status")
    if status is not None and status != "success":
        message = result.get("message", "no detail")
        raise OpsReadFailure(
            f"Gateway tool {tool_name!r} unavailable: {message}",
            tool=tool_name,
            reason="gateway_tool_unavailable",
        )
    data = result.get("data")
    if isinstance(data, dict):
        return data
    # A bare (already-unwrapped) mapping: return it as-is.
    return result


def _extract_tool_json(result: Any) -> Any:
    """Extract and JSON-decode the payload from an MCP tool-call result.

    Strands' ``MCPClient.call_tool_sync`` returns an MCP result whose ``content``
    is a list of content blocks; the shared tool Lambda returns its payload as a
    single text block of JSON. This reads that text defensively (handling both
    dict-shaped and object-shaped results across SDK versions) and decodes it
    into native Python.

    Args:
        result: The raw MCP tool-call result.

    Returns:
        The decoded JSON payload (dict or list), or the raw text when the text
        is not valid JSON.

    Raises:
        OpsReadFailure: If the result carries no readable text content, or the
            tool reported an error.
    """
    # Content may be attribute-style (result.content) or dict-style
    # (result["content"]); normalize to a list of blocks.
    content = None
    if isinstance(result, dict):
        content = result.get("content")
        if result.get("isError"):
            raise OpsReadFailure(
                f"Gateway tool reported an error: {content!r}",
                reason="gateway_tool_error",
            )
    else:
        content = getattr(result, "content", None)
        if getattr(result, "isError", False):
            raise OpsReadFailure(
                f"Gateway tool reported an error: {content!r}",
                reason="gateway_tool_error",
            )

    if not content:
        raise OpsReadFailure(
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
        raise OpsReadFailure(
            "Gateway tool result had no text content", reason="gateway_tool_error"
        )

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # A non-JSON string result is returned as-is; callers handle the shape.
        return text


class GatewayToolClient:
    """An open MCP connection to the shared StayOS Gateway (context manager).

    Mirrors the Triage Agent's connection: an ``aws_iam_streamablehttp_client``
    transport (SigV4, service ``bedrock-agentcore``) wrapped in a Strands
    ``MCPClient`` whose context stays open for the duration of one facade
    request. Use as::

        with GatewayToolClient(endpoint, region) as gateway:
            vips = gateway.call_tool("get_vip_guests", {"propertyId": pid})

    Attributes:
        endpoint: The Gateway MCP endpoint URL.
        region: The AWS region for the SigV4 signer.
        aws_service: The signing service name (``bedrock-agentcore``).
    """

    def __init__(
        self, endpoint: str, region: str, aws_service: str = GATEWAY_AWS_SERVICE
    ) -> None:
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
        # Map of bare tool name (e.g. "get_vip_guests") -> the actual name the
        # Gateway exposes over MCP. AgentCore Gateway namespaces a Lambda
        # target's tools as "<targetName>___<toolName>" (e.g.
        # "tools___get_vip_guests"), so a direct call by the BARE name is
        # rejected by the MCP server -> the tool call fails and this facade
        # degrades to empty VIPs/Ops data (BUG-031, the ops-read twin of the
        # triage-agent BUG-028). Populated lazily from discover_tools().
        self._tool_name_map: dict[str, str] = {}

    def connect(self) -> None:
        """Open the Gateway MCP connection (SigV4/IAM, Streamable HTTP).

        Imports the transport and MCP client lazily so this module imports
        cleanly in environments where ``mcp_proxy_for_aws`` / ``strands`` are not
        installed (the unit tests never open a real connection).

        Raises:
            OpsReadFailure: If the connection cannot be established.
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
            # the multiple tool calls one facade request makes.
            self._mcp_client.__enter__()
        except Exception as error:  # noqa: BLE001 - any connect error -> facade failure
            raise OpsReadFailure(
                f"Failed to connect to the shared Gateway: {error}",
                reason="gateway_connect_error",
            ) from error

    def discover_tools(self) -> list[Any]:
        """List the Gateway tools (``tools/list`` via MCP) and map their names.

        Records a bare-name -> actual-name map so :meth:`call_tool` can be called
        with a bare tool name (e.g. ``get_vip_guests``) even though the Gateway
        exposes it namespaced (e.g. ``tools___get_vip_guests``). The facade does
        not build a Strands Agent, so the returned list is only used to populate
        the map; callers normally rely on :meth:`call_tool` triggering discovery
        lazily.

        Returns:
            The discovered tool objects.

        Raises:
            OpsReadFailure: If the connection is not open.
        """
        if self._mcp_client is None:
            raise OpsReadFailure(
                "Gateway connection is not open", reason="gateway_connect_error"
            )
        tools = self._mcp_client.list_tools_sync()
        # Build the bare -> actual name map. Discovered tools may expose their
        # name as ``.tool_name`` (Strands) or ``.name`` (raw MCP); handle both.
        name_map: dict[str, str] = {}
        for tool in tools:
            actual = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
            if not isinstance(actual, str) or not actual:
                continue
            # The bare name is the segment after the "<target>___" prefix.
            bare = actual.rsplit("___", 1)[-1]
            name_map[bare] = actual
            name_map[actual] = actual
        self._tool_name_map = name_map
        return tools

    def _resolve_tool_name(self, tool_name: str) -> str:
        """Resolve a (possibly bare) tool name to the Gateway's actual name.

        Triggers a one-time :meth:`discover_tools` when the name map is empty so
        the facade never needs an explicit discovery call. Falls back to the
        input name unchanged when discovery yields no match (so behavior is
        unchanged against a Gateway that does not namespace).

        Args:
            tool_name: The tool name the caller passed (typically bare, e.g.
                ``get_vip_guests``).

        Returns:
            The namespaced name the Gateway exposes when known (e.g.
            ``tools___get_vip_guests``), else the input unchanged.
        """
        if not self._tool_name_map:
            # Best-effort lazy discovery; if it fails, fall through to the bare
            # name (the subsequent call_tool will surface any real error).
            try:
                self.discover_tools()
            except Exception as error:  # noqa: BLE001 - discovery is best-effort
                logger.warning(
                    "Gateway tool discovery failed; using bare tool name",
                    extra={"tool_name": tool_name, "error": str(error)},
                )
        return self._tool_name_map.get(tool_name, tool_name)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one Gateway tool and return its parsed JSON result.

        Args:
            tool_name: The Gateway tool name (e.g. ``get_vip_guests``).
            arguments: The tool arguments; MUST include ``propertyId`` so the
                Gateway scopes the result server-side.

        Returns:
            The tool's decoded JSON result (still the ``{status, data}`` envelope;
            call :func:`tool_data` to unwrap it).

        Raises:
            OpsReadFailure: If the connection is not open or the tool call fails.
        """
        if self._mcp_client is None:
            raise OpsReadFailure(
                "Gateway connection is not open", reason="gateway_connect_error"
            )
        # Resolve a bare name (e.g. "get_vip_guests") to the Gateway's actual
        # namespaced name (e.g. "tools___get_vip_guests"); see BUG-031 (the
        # ops-read twin of triage BUG-028). Discovery runs lazily on first call.
        resolved_name = self._resolve_tool_name(tool_name)
        logger.info(
            "Ops-read facade invoking Gateway tool",
            extra={
                "tool_name": tool_name,
                "resolved_tool_name": resolved_name,
                "property_id": arguments.get("propertyId"),
            },
        )
        try:
            result = self._mcp_client.call_tool_sync(
                tool_use_id=f"ops-read-{tool_name}",
                name=resolved_name,
                arguments=arguments,
            )
        except OpsReadFailure:
            raise
        except Exception as error:  # noqa: BLE001 - any tool error -> facade failure
            raise OpsReadFailure(
                f"Gateway tool {tool_name!r} invocation failed: {error}",
                tool=tool_name,
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


__all__ = [
    "GATEWAY_AWS_SERVICE",
    "ToolCaller",
    "GatewayToolClient",
    "tool_data",
    "_extract_tool_json",
]
