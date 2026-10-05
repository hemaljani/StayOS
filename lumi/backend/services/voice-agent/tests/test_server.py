"""Unit tests for the BedrockAgentCoreApp-based WebSocket server (server.py).

Tests validate the /ping health status reporting (Healthy vs HealthyBusy),
the identity message processing flow (_process_identity_message), and message
routing by type. Uses unittest.mock to mock the BedrockAgentCoreApp framework,
Starlette WebSocket, and identity resolver without requiring real AWS connections.

Validates: Requirements 1.2, 1.3, 6.3, 6.5
"""

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the voice-agent service directory to the path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Mock external SDK modules that are not installed in the test environment.
# Must happen before importing server.py which imports nova_sonic_session.
# ---------------------------------------------------------------------------

# Mock the bedrock-agentcore SDK module and its sub-modules.
# The BedrockAgentCoreApp mock must let @app.ping and @app.websocket decorators
# pass through the decorated function unchanged so tests can call them directly.
_mock_agentcore_module = ModuleType("bedrock_agentcore")


class _MockBedrockAgentCoreApp:
    """Mock BedrockAgentCoreApp that preserves decorated functions."""

    def ping(self, func: Any) -> Any:
        """Pass-through decorator for the ping handler function."""
        return func

    def websocket(self, func: Any) -> Any:
        """Pass-through decorator for the websocket handler function."""
        return func

    def run(self, **kwargs: Any) -> None:
        """No-op run for testing."""


_mock_agentcore_module.BedrockAgentCoreApp = _MockBedrockAgentCoreApp

_mock_agentcore_runtime_module = ModuleType("bedrock_agentcore.runtime")
_mock_agentcore_runtime_models_module = ModuleType("bedrock_agentcore.runtime.models")


class _MockPingStatus:
    """Mock PingStatus enum matching the bedrock-agentcore SDK."""

    HEALTHY = "Healthy"
    HEALTHY_BUSY = "HealthyBusy"


_mock_agentcore_runtime_models_module.PingStatus = _MockPingStatus

sys.modules.setdefault("bedrock_agentcore", _mock_agentcore_module)
sys.modules.setdefault("bedrock_agentcore.runtime", _mock_agentcore_runtime_module)
sys.modules.setdefault(
    "bedrock_agentcore.runtime.models", _mock_agentcore_runtime_models_module
)

# Mock the Smithy-based Bedrock runtime SDK modules
_mock_bedrock_client_module = ModuleType("aws_sdk_bedrock_runtime.client")
_mock_bedrock_client_module.BedrockRuntimeClient = MagicMock
_mock_bedrock_client_module.InvokeModelWithBidirectionalStreamOperationInput = (
    MagicMock
)

_mock_bedrock_config_module = ModuleType("aws_sdk_bedrock_runtime.config")
_mock_bedrock_config_module.Config = MagicMock

_mock_bedrock_models_module = ModuleType("aws_sdk_bedrock_runtime.models")
_mock_bedrock_models_module.BidirectionalInputPayloadPart = MagicMock
_mock_bedrock_models_module.InvokeModelWithBidirectionalStreamInputChunk = MagicMock

_mock_smithy_module = ModuleType("smithy_aws_core")
_mock_smithy_identity_module = ModuleType("smithy_aws_core.identity")
_mock_smithy_env_module = ModuleType("smithy_aws_core.identity.environment")
_mock_smithy_env_module.EnvironmentCredentialsResolver = MagicMock

sys.modules.setdefault(
    "aws_sdk_bedrock_runtime", ModuleType("aws_sdk_bedrock_runtime")
)
sys.modules.setdefault("aws_sdk_bedrock_runtime.client", _mock_bedrock_client_module)
sys.modules.setdefault("aws_sdk_bedrock_runtime.config", _mock_bedrock_config_module)
sys.modules.setdefault("aws_sdk_bedrock_runtime.models", _mock_bedrock_models_module)
sys.modules.setdefault("smithy_aws_core", _mock_smithy_module)
sys.modules.setdefault("smithy_aws_core.identity", _mock_smithy_identity_module)
sys.modules.setdefault(
    "smithy_aws_core.identity.environment", _mock_smithy_env_module
)

# Mock starlette.websockets for type reference in server.py
_mock_starlette_module = ModuleType("starlette")
_mock_starlette_ws_module = ModuleType("starlette.websockets")
_mock_starlette_ws_module.WebSocket = MagicMock
_mock_starlette_ws_module.WebSocketDisconnect = type(
    "WebSocketDisconnect", (Exception,), {}
)

sys.modules.setdefault("starlette", _mock_starlette_module)
sys.modules.setdefault("starlette.websockets", _mock_starlette_ws_module)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables expected by server.py and its imports.

    Patches table names and region so modules can be imported and run
    without real AWS configuration.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "stayos-settings")
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", "stayos-reservations")
    monkeypatch.setenv("ROOMS_TABLE_NAME", "stayos-rooms")
    monkeypatch.setenv("GUESTS_TABLE_NAME", "stayos-guests")
    monkeypatch.setenv("REVENUES_TABLE_NAME", "stayos-revenues")
    monkeypatch.setenv("WORK_ORDERS_TABLE_NAME", "stayos-work-orders")


@pytest.fixture()
def mock_websocket() -> AsyncMock:
    """Create a mock Starlette WebSocket for testing server handlers.

    Provides an AsyncMock WebSocket with the expected methods (accept,
    receive_text, send_json, close) matching the Starlette WebSocket API.

    Returns:
        AsyncMock configured to behave like a Starlette WebSocket instance.
    """
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture()
def mock_session() -> AsyncMock:
    """Create a mock NovaSonicSession for testing server.py handlers.

    Provides an AsyncMock session with the expected attributes and methods
    so the WebSocket handler can interact with it without a real Bedrock
    connection.

    Returns:
        AsyncMock configured to behave like a NovaSonicSession instance.
    """
    session = AsyncMock()
    session.start = AsyncMock()
    session.send_audio = AsyncMock()
    session.close = AsyncMock()
    session.handle_output_events = AsyncMock()
    session.is_idle = MagicMock(return_value=False)

    # Mock the context dataclass attributes
    session.context = MagicMock()
    session.context.connection_id = "test-conn-123"
    session.context.is_stream_active = True
    session.context.ws = AsyncMock()
    session.context.ws.send_json = AsyncMock()
    session.context.ws.close = AsyncMock()

    return session


# ---------------------------------------------------------------------------
# Test: Ping Status (GET /ping)
# Validates: Requirements 1.2, 1.3
# ---------------------------------------------------------------------------


class TestPingHandler:
    """Tests for the /ping health check endpoint status reporting.

    The AgentCore protocol contract requires GET /ping to return Healthy
    when no active sessions exist, and HealthyBusy while a voice session
    is active. This prevents AgentCore from terminating the container
    during an active conversation.

    Validates: Requirements 1.2, 1.3
    """

    def test_ping_returns_healthy_with_no_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ping returns PingStatus.HEALTHY when _active_sessions is empty.

        When no voice session is active, the container signals to AgentCore
        that it can be reclaimed after the platform idle timeout.
        Validates: Requirement 1.2
        """
        import server

        # Ensure no active sessions
        monkeypatch.setattr(server, "_active_sessions", set())

        result = server.ping_handler()

        assert result == _MockPingStatus.HEALTHY

    def test_ping_returns_healthy_busy_with_active_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_session: AsyncMock,
    ) -> None:
        """Ping returns PingStatus.HEALTHY_BUSY when _active_sessions has entries.

        While a voice session is active, the container signals HealthyBusy
        to prevent AgentCore from terminating it prematurely.
        Validates: Requirement 1.3
        """
        import server

        # Add a mock session to the active sessions set
        monkeypatch.setattr(server, "_active_sessions", {mock_session})

        result = server.ping_handler()

        assert result == _MockPingStatus.HEALTHY_BUSY

    def test_ping_returns_healthy_after_session_removed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_session: AsyncMock,
    ) -> None:
        """Ping transitions from HealthyBusy back to Healthy when session ends.

        After the session is removed from the active set (e.g., client
        disconnects, idle timeout fires), ping status reverts to Healthy.
        Validates: Requirement 1.2, 1.3
        """
        import server

        # Start with an active session
        active_set = {mock_session}
        monkeypatch.setattr(server, "_active_sessions", active_set)

        # Confirm HealthyBusy while active
        assert server.ping_handler() == _MockPingStatus.HEALTHY_BUSY

        # Remove the session (simulating session end)
        active_set.discard(mock_session)

        # Should now be Healthy
        assert server.ping_handler() == _MockPingStatus.HEALTHY


# ---------------------------------------------------------------------------
# Test: Identity Message Processing (_process_identity_message)
# Validates: Requirements 6.3, 6.5
# ---------------------------------------------------------------------------


class TestProcessIdentityMessage:
    """Tests for the first WebSocket message identity verification.

    The server expects the first WebSocket message to be a JSON payload
    with type 'identity' and an accessToken field. The token is validated
    via resolve_identity() which calls Cognito GetUser.

    Validates: Requirements 6.3, 6.5
    """

    @pytest.mark.asyncio
    async def test_valid_identity_message_returns_claims(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """Valid identity message with accessToken returns property_id and gm_alias.

        When the browser sends a well-formed identity message as the first
        WebSocket message and resolve_identity succeeds, the function returns
        a dict with property_id and gm_alias for session creation.
        Validates: Requirement 6.3
        """
        import server

        # Configure WebSocket to return a valid identity message
        identity_payload = json.dumps({
            "type": "identity",
            "accessToken": "valid-access-token-123",
        })
        mock_websocket.receive_text.return_value = identity_payload

        # Mock resolve_identity to return valid claims
        with patch(
            "server.resolve_identity",
            new_callable=AsyncMock,
            return_value={"property_id": "PROP-BEACH-42", "gm_alias": "gm-carlos"},
        ):
            result = await server._process_identity_message(mock_websocket)

        assert result is not None
        assert result["property_id"] == "PROP-BEACH-42"
        assert result["gm_alias"] == "gm-carlos"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none_and_closes(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """Invalid access token results in WebSocket close with IDENTITY_FAILED error.

        When resolve_identity raises IdentityError (expired, revoked, or
        invalid token), the function sends an error event to the browser,
        closes the WebSocket with code 1008, and returns None.
        Validates: Requirement 6.5
        """
        import server
        from identity_resolver import IdentityError

        # Configure WebSocket to return an identity message with bad token
        identity_payload = json.dumps({
            "type": "identity",
            "accessToken": "expired-token-xyz",
        })
        mock_websocket.receive_text.return_value = identity_payload

        # Mock resolve_identity to raise IdentityError
        with patch(
            "server.resolve_identity",
            new_callable=AsyncMock,
            side_effect=IdentityError(
                "Access token is invalid or expired", reason="token_invalid"
            ),
        ):
            result = await server._process_identity_message(mock_websocket)

        assert result is None

        # Verify error event was sent to the browser
        mock_websocket.send_json.assert_called_once()
        error_event = mock_websocket.send_json.call_args[0][0]
        assert error_event["type"] == "error"
        assert error_event["code"] == "IDENTITY_FAILED"

        # Verify WebSocket was closed with policy violation code
        mock_websocket.close.assert_called_once_with(
            code=1008, reason="IDENTITY_FAILED"
        )

    @pytest.mark.asyncio
    async def test_non_identity_type_returns_none_and_closes(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """First message with wrong type results in WebSocket close with error.

        If the first message is valid JSON but has a type other than 'identity',
        the function rejects it, sends an error event, and returns None.
        Validates: Requirement 6.3
        """
        import server

        # Configure WebSocket to return a non-identity message type
        wrong_type_payload = json.dumps({
            "type": "audioInput",
            "audioData": "SGVsbG8=",
        })
        mock_websocket.receive_text.return_value = wrong_type_payload

        result = await server._process_identity_message(mock_websocket)

        assert result is None

        # Verify error event was sent
        mock_websocket.send_json.assert_called_once()
        error_event = mock_websocket.send_json.call_args[0][0]
        assert error_event["type"] == "error"
        assert error_event["code"] == "IDENTITY_FAILED"

        # Verify WebSocket was closed
        mock_websocket.close.assert_called_once_with(
            code=1008, reason="IDENTITY_FAILED"
        )

    @pytest.mark.asyncio
    async def test_missing_access_token_returns_none_and_closes(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """Identity message without accessToken field results in WebSocket close.

        If the identity message has the correct type but lacks the accessToken
        field (or it is empty), the function rejects it without calling
        resolve_identity.
        Validates: Requirement 6.3
        """
        import server

        # Configure WebSocket to return identity message with empty accessToken
        missing_token_payload = json.dumps({
            "type": "identity",
            "accessToken": "",
        })
        mock_websocket.receive_text.return_value = missing_token_payload

        result = await server._process_identity_message(mock_websocket)

        assert result is None

        # Verify error event was sent
        mock_websocket.send_json.assert_called_once()
        error_event = mock_websocket.send_json.call_args[0][0]
        assert error_event["type"] == "error"
        assert error_event["code"] == "IDENTITY_FAILED"

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none_and_closes(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """Malformed JSON as first message results in WebSocket close with error.

        If the first WebSocket message cannot be parsed as JSON, the function
        sends an error event and closes the connection.
        Validates: Requirement 6.3
        """
        import server

        # Configure WebSocket to return invalid JSON
        mock_websocket.receive_text.return_value = "this is not valid json {{{"

        result = await server._process_identity_message(mock_websocket)

        assert result is None

        # Verify error event was sent
        mock_websocket.send_json.assert_called_once()
        error_event = mock_websocket.send_json.call_args[0][0]
        assert error_event["type"] == "error"
        assert error_event["code"] == "IDENTITY_FAILED"

    @pytest.mark.asyncio
    async def test_websocket_disconnect_before_identity_returns_none(
        self,
        mock_websocket: AsyncMock,
    ) -> None:
        """WebSocket disconnect before identity message returns None without error.

        If the client disconnects before sending the first message, the function
        returns None gracefully without attempting to send an error event.
        Validates: Requirement 6.3
        """
        import server
        from starlette.websockets import WebSocketDisconnect

        # Configure WebSocket to raise disconnect on receive
        mock_websocket.receive_text.side_effect = WebSocketDisconnect()

        result = await server._process_identity_message(mock_websocket)

        assert result is None

        # No error event should be sent since the client already disconnected
        mock_websocket.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Message Routing (_message_loop)
# Validates: Requirements 6.3, 6.5
# ---------------------------------------------------------------------------


class TestMessageRouting:
    """Tests for routing incoming WebSocket messages by type field.

    After identity verification, the message loop routes incoming messages
    by their 'type' field: audioInput forwards audio to Nova Sonic,
    sessionEnd closes the session gracefully, and unknown types are logged.

    Validates: Requirements 6.3, 6.5
    """

    @pytest.mark.asyncio
    async def test_audio_input_routes_to_session_send_audio(
        self,
        mock_websocket: AsyncMock,
        mock_session: AsyncMock,
    ) -> None:
        """audioInput message type forwards audio data to the Nova Sonic session.

        The message loop extracts audioData from the message and calls
        session.send_audio with it for forwarding to the Bedrock stream.
        Validates: Requirement 6.3
        """
        import server
        from starlette.websockets import WebSocketDisconnect

        # First call returns an audioInput message, second call disconnects
        mock_websocket.receive_text.side_effect = [
            json.dumps({"type": "audioInput", "audioData": "SGVsbG8gV29ybGQ="}),
            WebSocketDisconnect(),
        ]

        # Run message loop (will exit on WebSocketDisconnect)
        with pytest.raises(WebSocketDisconnect):
            await server._message_loop(mock_session, mock_websocket)

        # Verify audio was forwarded to the session
        mock_session.send_audio.assert_called_once_with("SGVsbG8gV29ybGQ=")

    @pytest.mark.asyncio
    async def test_session_end_closes_session_gracefully(
        self,
        mock_websocket: AsyncMock,
        mock_session: AsyncMock,
    ) -> None:
        """sessionEnd message type triggers graceful session close.

        When the client sends sessionEnd, the message loop closes the Nova
        Sonic stream, sends a sessionEnded confirmation, and closes the WebSocket.
        Validates: Requirement 6.5
        """
        import server

        # Client sends sessionEnd message
        mock_websocket.receive_text.return_value = json.dumps(
            {"type": "sessionEnd"}
        )

        await server._message_loop(mock_session, mock_websocket)

        # Verify session.close() was called
        mock_session.close.assert_called_once()

        # Verify sessionEnded confirmation was sent
        mock_websocket.send_json.assert_called_once_with({
            "type": "sessionEnded",
            "reason": "explicit",
        })

        # Verify WebSocket was closed
        mock_websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_unrecognized_type_continues_without_crash(
        self,
        mock_websocket: AsyncMock,
        mock_session: AsyncMock,
    ) -> None:
        """Unrecognized message type is logged and does not crash the session.

        Unknown message types are ignored (with a warning log) so that the
        session remains active for subsequent valid messages.
        Validates: Requirement 6.3
        """
        import server
        from starlette.websockets import WebSocketDisconnect

        # First: unknown type, second: valid audioInput, third: disconnect
        mock_websocket.receive_text.side_effect = [
            json.dumps({"type": "unknownType", "data": "foo"}),
            json.dumps({"type": "audioInput", "audioData": "dGVzdA=="}),
            WebSocketDisconnect(),
        ]

        with pytest.raises(WebSocketDisconnect):
            await server._message_loop(mock_session, mock_websocket)

        # Verify session continued processing after unknown type
        mock_session.send_audio.assert_called_once_with("dGVzdA==")

    @pytest.mark.asyncio
    async def test_malformed_json_continues_without_crash(
        self,
        mock_websocket: AsyncMock,
        mock_session: AsyncMock,
    ) -> None:
        """Malformed JSON in message loop is logged and session continues.

        Non-parseable messages are ignored so that a single corrupt frame
        does not terminate an active voice session.
        Validates: Requirement 6.3
        """
        import server
        from starlette.websockets import WebSocketDisconnect

        # First: malformed JSON, second: valid audioInput, third: disconnect
        mock_websocket.receive_text.side_effect = [
            "not valid json {{{",
            json.dumps({"type": "audioInput", "audioData": "YWJj"}),
            WebSocketDisconnect(),
        ]

        with pytest.raises(WebSocketDisconnect):
            await server._message_loop(mock_session, mock_websocket)

        # Verify session continued processing after malformed JSON
        mock_session.send_audio.assert_called_once_with("YWJj")

    @pytest.mark.asyncio
    async def test_audio_input_with_empty_data_is_ignored(
        self,
        mock_websocket: AsyncMock,
        mock_session: AsyncMock,
    ) -> None:
        """audioInput message with empty audioData does not call send_audio.

        An audioInput message with no audio payload is silently skipped to
        avoid sending empty frames to the Nova Sonic stream.
        Validates: Requirement 6.3
        """
        import server
        from starlette.websockets import WebSocketDisconnect

        # audioInput with empty audioData, then disconnect
        mock_websocket.receive_text.side_effect = [
            json.dumps({"type": "audioInput", "audioData": ""}),
            WebSocketDisconnect(),
        ]

        with pytest.raises(WebSocketDisconnect):
            await server._message_loop(mock_session, mock_websocket)

        # send_audio should NOT have been called with empty data
        mock_session.send_audio.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Idle Timeout Monitor
# Validates: Requirement 6.5
# ---------------------------------------------------------------------------


class TestIdleTimeoutMonitor:
    """Tests for the idle timeout monitor (60s inactivity threshold).

    The _idle_monitor coroutine checks session.is_idle() periodically.
    When the session exceeds the idle threshold, it sends a sessionEnded
    event and closes both the Nova Sonic stream and the WebSocket.

    Validates: Requirement 6.5
    """

    @pytest.mark.asyncio
    async def test_idle_timeout_closes_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_websocket: AsyncMock,
    ) -> None:
        """When is_idle returns True, the monitor closes the session.

        The idle monitor detects inactivity, sends a sessionEnded event
        with reason 'idle_timeout', closes the Nova Sonic stream, and
        closes the WebSocket connection.
        Validates: Requirement 6.5
        """
        import server

        # Create a session that reports idle immediately
        idle_session = AsyncMock()
        idle_session.is_idle = MagicMock(return_value=True)
        idle_session.close = AsyncMock()
        idle_session.context = MagicMock()
        idle_session.context.connection_id = "idle-conn-001"
        idle_session.context.is_stream_active = True

        # Use a 0-second interval for fast test execution
        monkeypatch.setattr(server, "IDLE_CHECK_INTERVAL_SECONDS", 0)

        await server._idle_monitor(idle_session, mock_websocket)

        # Verify sessionEnded was sent to the browser
        mock_websocket.send_json.assert_called_with({
            "type": "sessionEnded",
            "reason": "idle_timeout",
        })

        # Verify session stream was closed
        idle_session.close.assert_called_once()

        # Verify WebSocket was closed
        mock_websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_active_session_not_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_websocket: AsyncMock,
    ) -> None:
        """Session remains open when is_idle returns False.

        The idle monitor should continue checking without closing the session
        as long as the session has recent activity.
        Validates: Requirement 6.5
        """
        import server

        # Session that is never idle - we stop it by deactivating the stream
        active_session = AsyncMock()
        active_session.is_idle = MagicMock(return_value=False)
        active_session.close = AsyncMock()
        active_session.context = MagicMock()
        active_session.context.connection_id = "active-conn-001"
        active_session.context.is_stream_active = True

        monkeypatch.setattr(server, "IDLE_CHECK_INTERVAL_SECONDS", 0)

        # Stop the monitor after brief period by deactivating the stream
        async def stop_after_checks() -> None:
            """Deactivate stream after a brief delay to end the loop."""
            await asyncio.sleep(0.05)
            active_session.context.is_stream_active = False

        await asyncio.gather(
            server._idle_monitor(active_session, mock_websocket),
            stop_after_checks(),
        )

        # Verify session.close() was NOT called (session was active)
        active_session.close.assert_not_called()

        # Verify WebSocket was NOT closed
        mock_websocket.close.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Graceful Shutdown Sequence
# Validates: Requirement 1.7
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Tests for SIGTERM-triggered graceful shutdown sequence.

    When AgentCore signals container shutdown via SIGTERM, the server must:
    1. Set the _shutting_down flag to reject new connections
    2. Close all active Nova Sonic streams
    3. Send sessionEnded events to all connected browser clients
    4. Exit cleanly with SystemExit(0)

    Validates: Requirement 1.7
    """

    def test_shutdown_sets_shutting_down_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_handle_sigterm sets _shutting_down to True immediately.

        The SIGTERM handler sets the shutdown flag synchronously so that
        new WebSocket connections are rejected during the shutdown window.
        Validates: Requirement 1.7
        """
        import signal

        import server

        # Ensure flag starts as False
        monkeypatch.setattr(server, "_shutting_down", False)

        # Mock asyncio.get_event_loop to avoid scheduling the shutdown coroutine
        mock_loop = MagicMock()
        mock_loop.call_soon = MagicMock()
        with patch("server.asyncio.get_event_loop", return_value=mock_loop):
            server._handle_sigterm(signal.SIGTERM, None)

        assert server._shutting_down is True

    @pytest.mark.asyncio
    async def test_shutdown_closes_active_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_websocket: AsyncMock,
    ) -> None:
        """_trigger_shutdown closes all active sessions and sends sessionEnded.

        When SIGTERM fires, _trigger_shutdown iterates all sessions in the
        active set, sends a sessionEnded event with reason 'server_shutdown'
        to each browser client, closes the Nova Sonic stream, and closes the
        WebSocket connection.
        Validates: Requirement 1.7
        """
        import server

        # Create a mock session in the active sessions set
        shutdown_session = AsyncMock()
        shutdown_session.close = AsyncMock()
        shutdown_session.context = MagicMock()
        shutdown_session.context.connection_id = "shutdown-conn-001"
        shutdown_session.context.is_stream_active = True
        shutdown_session.context.ws = mock_websocket

        # Set the active sessions to contain our mock session
        monkeypatch.setattr(server, "_active_sessions", {shutdown_session})

        # _trigger_shutdown raises SystemExit(0) at the end
        with pytest.raises(SystemExit) as exc_info:
            await server._trigger_shutdown()

        assert exc_info.value.code == 0

        # Verify sessionEnded event was sent to the browser WebSocket
        mock_websocket.send_json.assert_called_with({
            "type": "sessionEnded",
            "reason": "server_shutdown",
        })

        # Verify the Nova Sonic stream was closed
        shutdown_session.close.assert_called_once()

        # Verify the WebSocket was closed
        mock_websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_with_no_sessions_exits_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_trigger_shutdown exits cleanly with SystemExit(0) when no sessions active.

        With an empty _active_sessions set, the shutdown sequence should skip
        session cleanup entirely and raise SystemExit(0) immediately.
        Validates: Requirement 1.7
        """
        import server

        # Ensure no active sessions exist
        monkeypatch.setattr(server, "_active_sessions", set())

        # _trigger_shutdown should raise SystemExit(0)
        with pytest.raises(SystemExit) as exc_info:
            await server._trigger_shutdown()

        assert exc_info.value.code == 0
