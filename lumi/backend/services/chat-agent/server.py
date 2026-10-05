"""BedrockAgentCoreApp WebSocket server for the StayOS Chat Agent.

Entry point for the AgentCore Runtime container. Bridges browser WebSocket
connections to a per-session Strands Agent backed by Claude Sonnet, which
answers General Manager questions about a single property's operational data
by calling the 5 read-only tools exposed through the AgentCore Gateway (via
MCP). Resolves user identity from the first WebSocket message (Cognito Access
Token via GetUser API) and manages session lifecycles including idle timeout
and graceful shutdown on SIGTERM.

Role in project: The main application server running on AgentCore Runtime for
the text chat experience. Implements the AgentCore protocol contract: GET
/ping for health status and WebSocket /ws for chat sessions. Creates one
ChatSession per accepted WebSocket connection, each holding its own Gateway
MCP connection and Strands Agent instance so conversations and tool
connections never leak across sessions.

The voice agent (backend/services/voice-agent) continues to call tools
directly, in-process, for latency reasons. This chat agent instead discovers
and invokes tools through the Gateway via MCP, so new tools registered on the
Gateway are picked up automatically without redeploying this service.

Environment variables:
    GATEWAY_ENDPOINT_URL: AgentCore Gateway MCP endpoint (Streamable HTTP, IAM auth)
    AWS_DEFAULT_REGION: AWS region for boto3 clients and the Gateway SigV4 signer
    COGNITO_USER_POOL_ID: Informational - the identity_resolver module validates
        tokens via Cognito GetUser, which is scoped to a pool implicitly by the
        Access Token itself, so this variable is not read directly by this module.
"""

import asyncio
import json
import os
import signal
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from aws_lambda_powertools import Logger
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from starlette.websockets import WebSocket, WebSocketDisconnect
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from strands.types.tools import AgentTool

from identity_resolver import IdentityError, resolve_identity
from system_prompt import SYSTEM_PROMPT

# Module-level logger for structured logging (works outside Lambda via Powertools)
logger: Logger = Logger(service="stayos-chat-agent")

# Server configuration constants
IDLE_CHECK_INTERVAL_SECONDS: int = 5
# Chat sessions are less bursty than voice audio streams, so idle timeout is
# longer than the voice agent's 60s to tolerate GMs pausing to read a response.
IDLE_TIMEOUT_SECONDS: int = 300
SHUTDOWN_TIMEOUT_SECONDS: int = 10

# Claude Sonnet model configuration for the reasoning/response-generation model.
CLAUDE_MODEL_ID: str = "us.anthropic.claude-sonnet-4-6"
MAX_TOKENS: int = 4096
MODEL_TEMPERATURE: float = 0.3

# AWS region for the Gateway SigV4 signer and Bedrock model invocation.
AWS_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# AgentCore Gateway MCP endpoint URL (Streamable HTTP transport, IAM auth).
# Injected by AgentCore Runtime from SSM /${StackPrefix}/gateway/endpoint-url.
GATEWAY_ENDPOINT_URL: str = os.environ.get("GATEWAY_ENDPOINT_URL", "")

# Active sessions registry for graceful shutdown tracking.
# On AgentCore, each microVM hosts exactly one session, but we keep the set
# for consistent shutdown logic and HealthyBusy reporting.
_active_sessions: Set["ChatSession"] = set()

# Shutdown flag - when True, new connections are rejected
_shutting_down: bool = False

# Initialize the BedrockAgentCoreApp (Starlette/Uvicorn under the hood)
app: BedrockAgentCoreApp = BedrockAgentCoreApp()


class PropertyScopedMCPTool(AgentTool):
    """Wraps a Gateway-discovered MCP tool so propertyId is injected automatically.

    The chat agent's system prompt intentionally never mentions propertyId to
    the model (see system_prompt.py) - the model should never need to know
    which property it is scoped to. This wrapper subclasses `AgentTool`
    (rather than only duck-typing its `tool_name`/`tool_spec`/`tool_type`
    properties and `stream()` method) because Strands'
    `ToolRegistry.process_tools()` recognizes valid tools via
    `isinstance(tool, AgentTool)` - `AgentTool` is a plain ABC, not a
    `typing.Protocol`, so structural/duck typing alone does not satisfy that
    check and such tools are silently dropped with an "unrecognized tool
    specification" warning instead of being registered. Subclassing merges
    `propertyId` into the tool_use input before delegating to the real tool
    returned by MCPClient.list_tools_sync().
    """

    def __init__(self, wrapped_tool: Any, property_id: str) -> None:
        """Initialize the wrapper around a single discovered Gateway tool.

        Args:
            wrapped_tool: The MCPAgentTool instance returned by
                MCPClient.list_tools_sync() for one Gateway-registered tool.
            property_id: The authenticated GM's property ID, injected into
                every invocation of this tool for the lifetime of the session.
        """
        super().__init__()
        self._wrapped_tool = wrapped_tool
        self._property_id = property_id

    @property
    def tool_name(self) -> str:
        """Return the underlying MCP tool's agent-facing name."""
        return self._wrapped_tool.tool_name

    @property
    def tool_spec(self) -> Any:
        """Return the underlying MCP tool's specification (name/description/inputSchema)."""
        return self._wrapped_tool.tool_spec

    @property
    def tool_type(self) -> str:
        """Return the underlying MCP tool's type identifier (always "python" for MCP tools)."""
        return self._wrapped_tool.tool_type

    async def stream(
        self,
        tool_use: Dict[str, Any],
        invocation_state: Dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Inject propertyId into the tool input, then delegate to the real tool.

        Args:
            tool_use: The tool invocation request (toolUseId, name, input) that
                the model produced. `input` never contains propertyId because
                the model was never told about it.
            invocation_state: Agent execution context, passed through unchanged.
            **kwargs: Additional keyword arguments forwarded to the wrapped tool.

        Yields:
            Tool events from the wrapped MCP tool, with the last event being
            the tool result that gets returned to the model.
        """
        scoped_tool_use = dict(tool_use)
        scoped_input = dict(tool_use.get("input", {}))
        scoped_input["propertyId"] = self._property_id
        scoped_tool_use["input"] = scoped_input

        async for event in self._wrapped_tool.stream(scoped_tool_use, invocation_state, **kwargs):
            yield event


class ChatSessionContext:
    """State held in memory for one chat session (one WebSocket connection).

    On AgentCore, each session runs in an isolated microVM, so there is
    exactly one ChatSessionContext per container instance.

    Attributes:
        connection_id: Unique ID for this WebSocket connection.
        property_id: From Cognito GetUser custom:propertyId (injected into every tool call).
        gm_alias: From Cognito GetUser custom:gmAlias (for logging and personalization).
        ws: Reference to the Starlette WebSocket for sending messages.
        last_activity: Timestamp of the last inbound message (for idle timeout).
    """

    def __init__(self, connection_id: str, property_id: str, gm_alias: str, ws: Any) -> None:
        """Initialize the session context.

        Args:
            connection_id: Unique ID for this WebSocket connection.
            property_id: The authenticated GM's property ID.
            gm_alias: The authenticated GM's alias.
            ws: The Starlette WebSocket connection for this session.
        """
        self.connection_id = connection_id
        self.property_id = property_id
        self.gm_alias = gm_alias
        self.ws = ws
        self.last_activity: float = time.time()


class ChatSession:
    """Holds the Strands Agent and Gateway MCP connection for one chat session.

    Each accepted WebSocket connection gets its own ChatSession, its own
    Gateway MCP connection, and its own Strands Agent instance. Multi-turn
    conversation memory is held entirely by the Agent instance in-process, so
    context never leaks between sessions and disappears entirely once the
    session is closed (matches the "no cross-session persistence" requirement).
    """

    def __init__(self, property_id: str, gm_alias: str, ws: Any) -> None:
        """Initialize a new chat session (does not open the Gateway connection yet).

        Args:
            property_id: The authenticated GM's property ID.
            gm_alias: The authenticated GM's alias.
            ws: The Starlette WebSocket connection for this session.
        """
        self.context: ChatSessionContext = ChatSessionContext(
            connection_id=str(uuid.uuid4()),
            property_id=property_id,
            gm_alias=gm_alias,
            ws=ws,
        )
        self._mcp_client: Optional[MCPClient] = None
        self.agent: Optional[Agent] = None

    def start(self) -> None:
        """Open the Gateway MCP connection and build the per-session Strands Agent.

        The Gateway uses AWS_IAM inbound authentication, so tool discovery and
        invocation requests are SigV4-signed via aws_iam_streamablehttp_client.
        The MCP client's context is entered manually (rather than via a `with`
        block) so the Gateway connection stays open for the entire WebSocket
        session - this lets multiple chat turns reuse the same connection and
        the same tool set instead of reconnecting on every message.

        Raises:
            Exception: Propagates any error from the MCP connection, tool
                discovery, or model setup so the caller can respond with a
                graceful AGENT_ERROR message and close the WebSocket.
        """
        self._mcp_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=GATEWAY_ENDPOINT_URL,
                aws_region=AWS_REGION,
                aws_service="bedrock-agentcore",
            )
        )
        self._mcp_client.__enter__()

        # tools/list against the Gateway - returns every registered tool with no
        # chat agent redeployment required when new tools are added later.
        discovered_tools: List[Any] = self._mcp_client.list_tools_sync()

        # Wrap every discovered tool so propertyId is injected automatically.
        scoped_tools: List[PropertyScopedMCPTool] = [
            PropertyScopedMCPTool(tool, self.context.property_id)
            for tool in discovered_tools
        ]

        model = BedrockModel(
            model_id=CLAUDE_MODEL_ID,
            max_tokens=MAX_TOKENS,
            temperature=MODEL_TEMPERATURE,
        )

        # callback_handler=None disables Strands' default console printer since
        # this server drives its own WebSocket streaming via stream_async().
        self.agent = Agent(
            model=model,
            tools=scoped_tools,
            system_prompt=SYSTEM_PROMPT,
            callback_handler=None,
        )

        logger.info(
            "Chat session started",
            extra={
                "connection_id": self.context.connection_id,
                "property_id": self.context.property_id,
                "gm_alias": self.context.gm_alias,
                "discovered_tool_count": len(discovered_tools),
            },
        )

    def close(self) -> None:
        """Close the Gateway MCP connection and release the Agent for this session."""
        if self._mcp_client is not None:
            try:
                self._mcp_client.__exit__(None, None, None)
            except Exception as error:
                logger.warning(
                    "Error closing MCP Gateway connection",
                    extra={
                        "connection_id": self.context.connection_id,
                        "error": str(error),
                    },
                )
            self._mcp_client = None

        self.agent = None

        logger.info(
            "Chat session ended",
            extra={
                "connection_id": self.context.connection_id,
                "property_id": self.context.property_id,
                "gm_alias": self.context.gm_alias,
            },
        )

    def touch(self) -> None:
        """Update the last-activity timestamp. Called on every inbound message."""
        self.context.last_activity = time.time()

    def is_idle(self) -> bool:
        """Return True if the session has been inactive beyond IDLE_TIMEOUT_SECONDS."""
        return (time.time() - self.context.last_activity) > IDLE_TIMEOUT_SECONDS


@app.ping
def ping_handler() -> PingStatus:
    """AgentCore health check endpoint (GET /ping).

    Returns HealthyBusy while any chat session is active, signaling AgentCore
    to keep the microVM alive. Returns Healthy when idle, allowing AgentCore to
    reclaim the VM after the platform idle timeout.

    Returns:
        PingStatus.HEALTHY_BUSY if active sessions exist.
        PingStatus.HEALTHY if no active sessions.
    """
    if _active_sessions:
        return PingStatus.HEALTHY_BUSY
    return PingStatus.HEALTHY


@app.websocket
async def websocket_handler(websocket: WebSocket, context: Any) -> None:
    """Handle a single chat agent WebSocket session.

    Implements the AgentCore WebSocket protocol contract on /ws port 8080.
    Each invocation corresponds to one GM chat session in an isolated microVM.

    Session lifecycle:
        1. Accept the WebSocket connection
        2. Receive the first message (identity verification payload)
        3. Extract propertyId and gmAlias via Cognito GetUser
        4. Open the Gateway MCP connection and build the per-session Strands Agent
        5. Run the message loop (message, sessionEnd routing)
        6. Clean up on disconnect or error

    Args:
        websocket: The Starlette WebSocket connection from AgentCore's proxy.
        context: AgentCore runtime context (session metadata, not used directly).

    Raises:
        No exceptions propagate - all errors are caught, logged, and result in
        WebSocket close with appropriate error messages.
    """
    # Reject new connections during shutdown
    if _shutting_down:
        logger.warning("Rejecting WebSocket during shutdown")
        await websocket.close(code=1013, reason="Server shutting down")
        return

    # Accept the WebSocket upgrade
    await websocket.accept()

    # Step 1: Receive and validate the identity message (first message)
    identity = await _process_identity_message(websocket)
    if identity is None:
        # Identity resolution failed - WebSocket already closed with error
        return

    property_id: str = identity["property_id"]
    gm_alias: str = identity["gm_alias"]

    logger.info(
        "WebSocket identity resolved",
        extra={"property_id": property_id, "gm_alias": gm_alias},
    )

    # Step 2: Create the chat session and register it before opening the
    # Gateway connection, so shutdown/cleanup logic sees it even if start() fails.
    session = ChatSession(property_id=property_id, gm_alias=gm_alias, ws=websocket)
    _active_sessions.add(session)

    idle_task: Optional[asyncio.Task[None]] = None

    try:
        # Step 3: Open the Gateway MCP connection and build the Strands Agent
        session.start()
    except Exception as error:
        logger.error(
            "Failed to start chat session (Gateway connection or model setup error)",
            extra={
                "connection_id": session.context.connection_id,
                "property_id": property_id,
                "error": str(error),
                "error_type": type(error).__name__,
            },
        )
        _active_sessions.discard(session)
        await _send_error_and_close(
            websocket,
            code="AGENT_ERROR",
            message="LUMI is temporarily unavailable. Please try again shortly.",
        )
        return

    try:
        # Notify browser that session is ready
        await websocket.send_json({"type": "sessionStarted"})

        # Start the idle timeout monitor as a background task
        idle_task = asyncio.create_task(_idle_monitor(session, websocket))

        # Message loop - route incoming WebSocket messages by type
        await _message_loop(session, websocket)

    except WebSocketDisconnect:
        logger.info(
            "WebSocket disconnected by client",
            extra={"connection_id": session.context.connection_id},
        )
    except Exception as error:
        logger.error(
            "Unexpected error in WebSocket handler",
            extra={
                "connection_id": session.context.connection_id,
                "error": str(error),
                "error_type": type(error).__name__,
            },
        )
    finally:
        # Cleanup: close the Gateway MCP connection, cancel background tasks,
        # and remove from active sessions registry
        await _cleanup_session(session, idle_task)


async def _process_identity_message(websocket: WebSocket) -> Optional[Dict[str, str]]:
    """Receive and validate the first WebSocket message as identity verification.

    The browser sends the Cognito Access Token as the first message after
    connection establishment. This function extracts the token and calls
    resolve_identity() to validate it via the Cognito GetUser API.

    Args:
        websocket: The active Starlette WebSocket connection.

    Returns:
        Dictionary with property_id and gm_alias on success, or None on failure.
        On failure, the WebSocket is closed with an error message.
    """
    try:
        # Wait for the first message (identity payload)
        raw_data = await websocket.receive_text()
        message: Dict[str, Any] = json.loads(raw_data)
    except WebSocketDisconnect:
        logger.warning("WebSocket disconnected before identity message")
        return None
    except json.JSONDecodeError:
        logger.warning("First WebSocket message is not valid JSON")
        await _send_error_and_close(
            websocket,
            code="IDENTITY_FAILED",
            message="First message must be valid JSON with type 'identity'.",
        )
        return None

    # Validate message type
    msg_type = message.get("type", "")
    if msg_type != "identity":
        logger.warning(
            "First WebSocket message is not an identity message",
            extra={"received_type": msg_type},
        )
        await _send_error_and_close(
            websocket,
            code="IDENTITY_FAILED",
            message="First message must have type 'identity' with an accessToken.",
        )
        return None

    # Extract the Access Token
    access_token: str = message.get("accessToken", "")
    if not access_token:
        logger.warning("Identity message missing accessToken field")
        await _send_error_and_close(
            websocket,
            code="IDENTITY_FAILED",
            message="Identity message must include a non-empty accessToken.",
        )
        return None

    # Validate the token via Cognito GetUser and extract claims
    try:
        identity = await resolve_identity(access_token)
    except IdentityError as exc:
        logger.warning(
            "Identity resolution failed",
            extra={"reason": exc.reason, "error": exc.message},
        )
        await _send_error_and_close(
            websocket,
            code="IDENTITY_FAILED",
            message="Authentication failed. Please sign in again.",
        )
        return None

    return identity


async def _send_error_and_close(
    websocket: WebSocket,
    code: str,
    message: str,
) -> None:
    """Send an error event to the browser and close the WebSocket.

    Used when identity verification fails, the agent fails to start, or other
    pre-session errors occur. Sends a structured error JSON message before
    closing the connection.

    Args:
        websocket: The active Starlette WebSocket connection.
        code: Machine-readable error code (e.g., "IDENTITY_FAILED", "AGENT_ERROR").
        message: Human-readable error description for the UI.
    """
    try:
        await websocket.send_json({
            "type": "error",
            "code": code,
            "message": message,
        })
        await websocket.close(code=1008, reason=code)
    except Exception as error:
        logger.warning(
            "Error sending error response before close",
            extra={"error": str(error)},
        )


def _extract_text_chunk(event: Dict[str, Any]) -> Optional[str]:
    """Extract a text delta from a Strands stream_async() event, if present.

    Strands Agent.stream_async() yields a sequence of event dicts covering
    lifecycle, tool-use, and text-generation events. Text generation chunks
    appear under the "data" key in the documented event shape. Other keys
    ("delta", "text") are checked defensively in case of SDK version
    differences, since only non-empty string values should ever be forwarded
    to the browser as a messageDelta.

    Args:
        event: One event dict yielded by Agent.stream_async().

    Returns:
        The text chunk if this event carries one, otherwise None.
    """
    for key in ("data", "delta", "text"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_tool_name(event: Dict[str, Any]) -> Optional[str]:
    """Extract the in-progress tool name from a Strands stream_async() event.

    Tool-use events carry a "current_tool_use" dict with a "name" field while
    the model is invoking a tool. Used only for structured logging context.

    Args:
        event: One event dict yielded by Agent.stream_async().

    Returns:
        The tool name if this event is a tool-use event, otherwise None.
    """
    current_tool_use = event.get("current_tool_use")
    if isinstance(current_tool_use, dict):
        tool_name = current_tool_use.get("name")
        if isinstance(tool_name, str) and tool_name:
            return tool_name
    return None


async def _handle_chat_message(
    session: ChatSession,
    websocket: WebSocket,
    user_text: str,
) -> None:
    """Run one conversation turn and stream the response as messageDelta events.

    Sends messageStart before invoking the agent, streams every text chunk
    produced by Agent.stream_async() as a messageDelta, and always sends
    messageEnd - even if the agent invocation raises - so the browser's typing
    indicator never gets stuck.

    Args:
        session: The ChatSession holding the per-session Strands Agent.
        websocket: The Starlette WebSocket connection for this session.
        user_text: The GM's message text for this turn.
    """
    if session.agent is None:
        await _send_error_and_close(
            websocket,
            code="AGENT_ERROR",
            message="LUMI is not ready. Please reconnect and try again.",
        )
        return

    await websocket.send_json({"type": "messageStart"})

    last_logged_tool_name: Optional[str] = None
    try:
        async for event in session.agent.stream_async(user_text):
            tool_name = _extract_tool_name(event)
            if tool_name and tool_name != last_logged_tool_name:
                last_logged_tool_name = tool_name
                logger.info(
                    "Chat agent invoking Gateway tool",
                    extra={
                        "connection_id": session.context.connection_id,
                        "property_id": session.context.property_id,
                        "tool_name": tool_name,
                    },
                )

            text_chunk = _extract_text_chunk(event)
            if text_chunk:
                await websocket.send_json({"type": "messageDelta", "text": text_chunk})
    except Exception as error:
        logger.error(
            "Agent invocation failed",
            extra={
                "connection_id": session.context.connection_id,
                "property_id": session.context.property_id,
                "error": str(error),
                "error_type": type(error).__name__,
            },
        )
        await websocket.send_json({
            "type": "error",
            "code": "AGENT_ERROR",
            "message": "LUMI ran into a problem answering that. Please try again.",
        })
    finally:
        await websocket.send_json({"type": "messageEnd"})


async def _message_loop(session: ChatSession, websocket: WebSocket) -> None:
    """Run the WebSocket message loop, routing messages by type.

    Continuously receives text messages from the WebSocket and dispatches
    them to the appropriate handler based on the 'type' field. Exits when
    the client disconnects (raises WebSocketDisconnect) or sends sessionEnd.

    Supported message types:
    - message: run one conversation turn and stream the response
    - sessionEnd: gracefully close the session

    Args:
        session: The ChatSession for this connection.
        websocket: The Starlette WebSocket connection.

    Raises:
        WebSocketDisconnect: When the client closes the connection.
    """
    while True:
        raw_data = await websocket.receive_text()

        try:
            message: Dict[str, Any] = json.loads(raw_data)
        except json.JSONDecodeError:
            logger.warning(
                "Received malformed JSON on WebSocket",
                extra={"connection_id": session.context.connection_id},
            )
            continue

        msg_type = message.get("type", "")
        session.touch()

        if msg_type == "message":
            # Run one conversation turn and stream the response
            user_text: str = message.get("text", "")
            if not user_text:
                logger.warning(
                    "Received message with empty text",
                    extra={"connection_id": session.context.connection_id},
                )
                continue
            await _handle_chat_message(session, websocket, user_text)

        elif msg_type == "sessionEnd":
            # Client requested session end - close gracefully
            logger.info(
                "Client requested session end",
                extra={"connection_id": session.context.connection_id},
            )
            await websocket.send_json({
                "type": "sessionEnded",
                "reason": "explicit",
            })
            await websocket.close()
            break

        else:
            # Unknown message type - log but don't crash
            logger.warning(
                "Unrecognized WebSocket message type",
                extra={
                    "connection_id": session.context.connection_id,
                    "message_type": msg_type,
                },
            )


async def _idle_monitor(
    session: ChatSession,
    websocket: WebSocket,
) -> None:
    """Monitor session activity and close after IDLE_TIMEOUT_SECONDS of inactivity.

    Checks the session's last_activity timestamp every IDLE_CHECK_INTERVAL_SECONDS.
    If the session has been idle beyond the threshold, sends a sessionEnded event
    to the browser and closes the WebSocket.

    Args:
        session: The ChatSession to monitor.
        websocket: The WebSocket connection to close on timeout.
    """
    try:
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)

            if session.is_idle():
                logger.info(
                    "Chat session idle timeout reached",
                    extra={"connection_id": session.context.connection_id},
                )
                # Notify browser before closing
                try:
                    await websocket.send_json({
                        "type": "sessionEnded",
                        "reason": "idle_timeout",
                    })
                except Exception as error:
                    logger.debug(
                        "Best-effort idle notification failed (WebSocket may already be closed)",
                        extra={
                            "connection_id": session.context.connection_id,
                            "error": str(error),
                        },
                    )

                try:
                    await websocket.close()
                except Exception as error:
                    logger.debug(
                        "Best-effort WebSocket close failed on idle timeout",
                        extra={
                            "connection_id": session.context.connection_id,
                            "error": str(error),
                        },
                    )

                break

    except asyncio.CancelledError:
        # Task cancelled during cleanup - expected behavior
        pass
    except Exception as error:
        logger.warning(
            "Error in idle monitor",
            extra={
                "connection_id": session.context.connection_id,
                "error": str(error),
            },
        )


async def _cleanup_session(
    session: ChatSession,
    idle_task: Optional[asyncio.Task[None]],
) -> None:
    """Clean up all resources for a disconnected or ended chat session.

    Closes the Gateway MCP connection (via session.close()), cancels the idle
    monitor task, and removes the session from the active sessions registry.

    Args:
        session: The ChatSession to clean up.
        idle_task: The idle monitor task (may be None).
    """
    logger.info(
        "Cleaning up chat session",
        extra={
            "connection_id": session.context.connection_id,
            "property_id": session.context.property_id,
        },
    )

    # Cancel the idle monitor task
    if idle_task and not idle_task.done():
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass

    # Close the Gateway MCP connection and release the Agent
    session.close()

    # Remove from the active sessions registry
    _active_sessions.discard(session)

    logger.info(
        "Chat session cleanup complete",
        extra={"connection_id": session.context.connection_id},
    )


def _handle_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM signal handler - triggers graceful shutdown via asyncio.

    Schedules the shutdown coroutine on the running event loop. This ensures
    the shutdown logic runs within the asyncio context where it can close
    WebSocket connections and Gateway MCP connections.

    AgentCore sends SIGTERM when replacing containers during deploys or when
    the platform idle timeout fires after application session closure.

    Args:
        signum: The signal number (always SIGTERM here).
        frame: The current stack frame (unused).
    """
    global _shutting_down
    _shutting_down = True
    logger.info("SIGTERM received, initiating graceful shutdown")

    # Schedule shutdown on the running event loop
    loop = asyncio.get_event_loop()
    loop.call_soon(lambda: asyncio.ensure_future(_trigger_shutdown()))


async def _trigger_shutdown() -> None:
    """Trigger the application shutdown by closing all active chat sessions.

    Called from the SIGTERM handler via the event loop. Notifies all connected
    clients, closes their Gateway MCP connections, then raises SystemExit.
    """
    logger.info(
        "Shutting down active chat sessions from SIGTERM",
        extra={"active_sessions": len(_active_sessions)},
    )

    if _active_sessions:
        close_tasks = [_shutdown_session(session) for session in list(_active_sessions)]
        try:
            await asyncio.wait_for(
                asyncio.gather(*close_tasks, return_exceptions=True),
                timeout=SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out, forcing exit")

    logger.info("All chat sessions closed, server shutting down")

    # Raise SystemExit to stop the Uvicorn runner
    raise SystemExit(0)


async def _shutdown_session(session: ChatSession) -> None:
    """Close a single chat session during graceful shutdown.

    Sends a sessionEnded event to the browser, closes the Gateway MCP
    connection, and closes the WebSocket connection.

    Args:
        session: The session to shut down.
    """
    try:
        # Notify the browser that the server is shutting down
        try:
            await session.context.ws.send_json({
                "type": "sessionEnded",
                "reason": "server_shutdown",
            })
        except Exception as error:
            logger.debug(
                "Best-effort shutdown notification failed",
                extra={
                    "connection_id": session.context.connection_id,
                    "error": str(error),
                },
            )

        # Close the Gateway MCP connection
        session.close()

        # Close the WebSocket
        try:
            await session.context.ws.close()
        except Exception as error:
            logger.debug(
                "Best-effort WebSocket close failed during shutdown",
                extra={
                    "connection_id": session.context.connection_id,
                    "error": str(error),
                },
            )

    except Exception as error:
        logger.warning(
            "Error closing chat session during shutdown",
            extra={
                "connection_id": session.context.connection_id,
                "error": str(error),
            },
        )
    finally:
        _active_sessions.discard(session)


if __name__ == "__main__":
    # Register SIGTERM handler for AgentCore graceful shutdown.
    # AgentCore sends SIGTERM when terminating the container (deploy rollover,
    # platform idle timeout, or manual stop).
    signal.signal(signal.SIGTERM, _handle_sigterm)

    if not GATEWAY_ENDPOINT_URL:
        logger.warning(
            "GATEWAY_ENDPOINT_URL is not set - chat sessions will fail to start"
        )

    logger.info(
        "Starting chat agent server on AgentCore Runtime",
        extra={"gateway_endpoint_configured": bool(GATEWAY_ENDPOINT_URL)},
    )

    # BedrockAgentCoreApp.run() starts Uvicorn on port 8080 with /ping and /ws
    app.run(log_level="info")
