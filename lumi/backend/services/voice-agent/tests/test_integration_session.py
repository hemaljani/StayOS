"""Integration tests for the WebSocket session lifecycle on AgentCore Runtime.

Tests validate the full end-to-end flow through server.py, identity_resolver.py,
and nova_sonic_session.py working together:
    connect → identity message → sessionStart → audioInput → sessionEnd

External dependencies (Nova Sonic, DynamoDB, Cognito) are mocked, but the
integration between modules is exercised to verify property-scoped access flows
correctly from identity resolution through session creation and tool dispatch.

Validates: Requirements 7.1, 7.2, 8.1, 8.6
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

# Mock starlette.websockets for type reference
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
    """Create a mock Starlette WebSocket for integration testing.

    Provides an AsyncMock WebSocket with the expected Starlette methods
    (accept, receive_text, send_json, send_text, close) and a closed
    property that defaults to False (connection is open).

    Returns:
        AsyncMock configured to behave like a Starlette WebSocket instance.
    """
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.closed = False
    return ws


@pytest.fixture()
def mock_nova_sonic_stream() -> AsyncMock:
    """Create a mock Nova Sonic bidirectional stream response.

    Simulates the response from invoke_model_with_bidirectional_stream.
    The input_stream allows sending events, and the output stream provides
    a channel that yields output events from the model.

    Returns:
        AsyncMock configured to behave like the Nova Sonic stream response.
    """
    stream_response = AsyncMock()
    # Mock input stream for sending events to Nova Sonic
    stream_response.input_stream = AsyncMock()
    stream_response.input_stream.send = AsyncMock()
    stream_response.input_stream.close = AsyncMock()

    # Mock output stream - returns an async iterable that yields events
    output_channel = AsyncMock()
    # Simulate stream closing immediately (no output events for lifecycle test)
    output_channel.receive = AsyncMock(side_effect=StopAsyncIteration())
    stream_response.await_output = AsyncMock(
        return_value=(None, output_channel)
    )

    return stream_response


# ---------------------------------------------------------------------------
# Integration Test: Full WebSocket Session Lifecycle
# Validates: Requirements 7.1, 7.2, 8.1, 8.6
# ---------------------------------------------------------------------------


class TestWebSocketSessionLifecycle:
    """Integration tests for the complete session lifecycle.

    Exercises the integration between server.py (WebSocket handler),
    identity_resolver.py (token validation), and nova_sonic_session.py
    (stream management). Verifies the property_id from identity resolution
    flows through to the session context and is used for property-scoped
    data access.

    Validates: Requirements 7.1, 7.2, 8.1, 8.6
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_connect_identity_audio_close(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full lifecycle: connect → identity → audio → sessionEnd.

        Exercises the complete happy-path flow:
        1. WebSocket connects and sends identity message with Access Token
        2. resolve_identity validates token, returns propertyId and gmAlias
        3. Language preference loaded from mocked DynamoDB settings table
        4. NovaSonicSession created with property_id from identity (not model)
        5. sessionStarted confirmation sent to browser
        6. audioInput message forwarded to Nova Sonic stream
        7. sessionEnd message triggers graceful close

        Validates: Requirements 7.1, 7.2, 8.1, 8.6
        """
        import server
        from nova_sonic_session import NovaSonicSession

        # Ensure no active sessions from prior tests
        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        # Configure WebSocket message sequence:
        # 1st receive_text: identity message
        # 2nd receive_text: audioInput message
        # 3rd receive_text: sessionEnd message
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "valid-cognito-access-token-xyz",
            }),
            json.dumps({
                "type": "audioInput",
                "audioData": "SGVsbG8gV29ybGQ=",
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock identity resolution (Cognito GetUser) - returns property claims
        mock_identity = {
            "property_id": "PROP-OCEANVIEW-101",
            "gm_alias": "gm-maria",
        }

        # Mock DynamoDB settings table response for language preference
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {
            "Item": {
                "gmAlias": "gm-maria",
                "audioPreferences": {"language": "es-US"},
            }
        }
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        # Track created session for assertions
        captured_session: Dict[str, Any] = {}

        # Patch NovaSonicSession to capture initialization params and mock stream
        original_init = NovaSonicSession.__init__

        def patched_init(
            self_inner: Any,
            property_id: str,
            gm_alias: str,
            language: str,
            ws: Any,
        ) -> None:
            """Capture session init params for assertion."""
            captured_session["property_id"] = property_id
            captured_session["gm_alias"] = gm_alias
            captured_session["language"] = language
            original_init(self_inner, property_id, gm_alias, language, ws)

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value=mock_identity,
            ) as mock_resolve,
            patch(
                "server._dynamodb_resource",
                mock_dynamodb_resource,
            ),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(
                NovaSonicSession, "__init__", patched_init
            ),
        ):
            # Execute the WebSocket handler
            await server.websocket_handler(mock_websocket, MagicMock())

        # --- Assertions ---

        # 1. WebSocket was accepted
        mock_websocket.accept.assert_called_once()

        # 2. Identity was resolved with the correct access token
        mock_resolve.assert_called_once_with("valid-cognito-access-token-xyz")

        # 3. Language preference was loaded from DynamoDB
        mock_dynamodb_resource.Table.assert_called_with("stayos-settings")
        mock_settings_table.get_item.assert_called_once_with(
            Key={"gmAlias": "gm-maria"}
        )

        # 4. Session was created with identity-derived property_id (Req 7.1)
        assert captured_session["property_id"] == "PROP-OCEANVIEW-101"
        assert captured_session["gm_alias"] == "gm-maria"
        assert captured_session["language"] == "es-US"

        # 5. sessionStarted confirmation was sent to browser (Req 8.6)
        sent_messages = [
            call[0][0] for call in mock_websocket.send_json.call_args_list
        ]
        assert {"type": "sessionStarted"} in sent_messages

        # 6. sessionEnded confirmation was sent after client requested close
        assert {"type": "sessionEnded", "reason": "explicit"} in sent_messages

    @pytest.mark.asyncio
    async def test_property_id_from_identity_not_model_params(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Property-scoped access uses propertyId from identity, never model params.

        Verifies that the NovaSonicSession context receives the property_id
        that was extracted from Cognito GetUser during identity resolution,
        and that this property_id is what would be passed to tool handlers
        (defense-in-depth: session context overrides any model-provided params).

        Validates: Requirements 7.1, 7.2
        """
        import server
        from nova_sonic_session import NovaSonicSession

        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        # The identity resolver returns PROP-HILLTOP-55
        identity_property_id = "PROP-HILLTOP-55"
        identity_gm_alias = "gm-jose"

        # Configure message sequence: identity → sessionEnd (minimal flow)
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "token-for-jose",
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock DynamoDB settings table (no language pref found → defaults to en-US)
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {"Item": {}}
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        # Capture the session's context after creation
        created_sessions: list = []

        original_init = NovaSonicSession.__init__

        def tracking_init(
            self_inner: Any,
            property_id: str,
            gm_alias: str,
            language: str,
            ws: Any,
        ) -> None:
            """Track session creation and capture context."""
            original_init(self_inner, property_id, gm_alias, language, ws)
            created_sessions.append(self_inner)

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value={
                    "property_id": identity_property_id,
                    "gm_alias": identity_gm_alias,
                },
            ),
            patch("server._dynamodb_resource", mock_dynamodb_resource),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(NovaSonicSession, "__init__", tracking_init),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # Verify the session was created
        assert len(created_sessions) == 1
        session = created_sessions[0]

        # The session context property_id MUST come from identity resolution
        assert session.context.property_id == identity_property_id
        assert session.context.gm_alias == identity_gm_alias

        # Default language should be en-US since settings table returned no pref
        assert session.context.language == "en-US"

    @pytest.mark.asyncio
    async def test_audio_input_forwarded_to_nova_sonic_stream(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audioInput messages are forwarded through the session to Nova Sonic.

        Verifies that audio data sent by the browser reaches the Nova Sonic
        bidirectional stream via the NovaSonicSession.send_audio method,
        confirming the stream remains open for multi-turn conversation.

        Validates: Requirement 8.1
        """
        import server
        from nova_sonic_session import NovaSonicSession

        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        audio_chunk_b64 = "dGVzdCBhdWRpbyBkYXRhIGNodW5r"

        # Configure message sequence: identity → audioInput → sessionEnd
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "token-audio-test",
            }),
            json.dumps({
                "type": "audioInput",
                "audioData": audio_chunk_b64,
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock DynamoDB settings table
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {"Item": {}}
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        # Track send_audio calls on the session
        audio_sent: list = []

        original_send_audio = NovaSonicSession.send_audio

        async def tracking_send_audio(self_inner: Any, audio_base64: str) -> None:
            """Track audio sent to the stream."""
            audio_sent.append(audio_base64)
            # Don't call original - it would try to use the real stream
            # Instead just verify the data was received

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value={
                    "property_id": "PROP-AUDIO-TEST",
                    "gm_alias": "gm-audio",
                },
            ),
            patch("server._dynamodb_resource", mock_dynamodb_resource),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(NovaSonicSession, "send_audio", tracking_send_audio),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # Verify audioInput was forwarded to the Nova Sonic session
        assert audio_chunk_b64 in audio_sent

    @pytest.mark.asyncio
    async def test_session_end_closes_stream_gracefully(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """sessionEnd message triggers graceful Nova Sonic stream close.

        When the browser sends a sessionEnd message, the server must:
        1. Close the Nova Sonic bidirectional stream
        2. Send sessionEnded confirmation to the browser
        3. Close the WebSocket connection
        4. Remove the session from the active sessions set

        Validates: Requirement 8.6
        """
        import server
        from nova_sonic_session import NovaSonicSession

        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        # Configure message sequence: identity → sessionEnd
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "token-close-test",
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock DynamoDB settings table
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {"Item": {}}
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        # Track session close calls
        close_called: list = []

        async def tracking_close(self_inner: Any) -> None:
            """Track that session close was invoked."""
            close_called.append(True)
            self_inner.context.is_stream_active = False
            self_inner._audio_content_started = False

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value={
                    "property_id": "PROP-CLOSE-TEST",
                    "gm_alias": "gm-close",
                },
            ),
            patch("server._dynamodb_resource", mock_dynamodb_resource),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(NovaSonicSession, "close", tracking_close),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # Verify session.close() was called (stream teardown)
        assert len(close_called) >= 1

        # Verify sessionEnded confirmation sent to browser
        sent_messages = [
            call[0][0] for call in mock_websocket.send_json.call_args_list
        ]
        assert {"type": "sessionEnded", "reason": "explicit"} in sent_messages

        # Verify WebSocket was closed
        mock_websocket.close.assert_called()

        # Verify session was removed from active sessions
        assert len(server._active_sessions) == 0

    @pytest.mark.asyncio
    async def test_session_removed_from_active_set_after_disconnect(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Session is removed from _active_sessions after lifecycle completes.

        After a session ends (whether by client request, disconnect, or error),
        the session must be removed from the _active_sessions set so that
        /ping returns Healthy and AgentCore can reclaim the container.

        Validates: Requirements 8.1, 8.6
        """
        import server
        from nova_sonic_session import NovaSonicSession

        active_set: set = set()
        monkeypatch.setattr(server, "_active_sessions", active_set)
        monkeypatch.setattr(server, "_shutting_down", False)

        # Configure message sequence: identity → sessionEnd
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "token-cleanup-test",
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock DynamoDB settings table
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {"Item": {}}
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value={
                    "property_id": "PROP-CLEANUP-TEST",
                    "gm_alias": "gm-cleanup",
                },
            ),
            patch("server._dynamodb_resource", mock_dynamodb_resource),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(
                NovaSonicSession,
                "close",
                AsyncMock(),
            ),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # After handler completes, active_sessions must be empty
        assert len(active_set) == 0

    @pytest.mark.asyncio
    async def test_language_preference_loaded_from_settings_table(
        self,
        mock_websocket: AsyncMock,
        mock_nova_sonic_stream: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GM language preference is read from DynamoDB settings table.

        The server reads the GM's language preference using the gmAlias from
        identity resolution and passes it to NovaSonicSession. If the table
        has a language set, that value should be used.

        Validates: Requirements 7.1, 8.1
        """
        import server
        from nova_sonic_session import NovaSonicSession

        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        # Configure message sequence: identity → sessionEnd
        mock_websocket.receive_text.side_effect = [
            json.dumps({
                "type": "identity",
                "accessToken": "token-lang-test",
            }),
            json.dumps({
                "type": "sessionEnd",
            }),
        ]

        # Mock DynamoDB settings table returns Japanese preference
        mock_settings_table = MagicMock()
        mock_settings_table.get_item.return_value = {
            "Item": {
                "gmAlias": "gm-tanaka",
                "audioPreferences": {"language": "ja-JP"},
            }
        }
        mock_dynamodb_resource = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_settings_table

        # Capture the language passed to session creation
        created_sessions: list = []
        original_init = NovaSonicSession.__init__

        def tracking_init(
            self_inner: Any,
            property_id: str,
            gm_alias: str,
            language: str,
            ws: Any,
        ) -> None:
            """Track session creation."""
            original_init(self_inner, property_id, gm_alias, language, ws)
            created_sessions.append(self_inner)

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                return_value={
                    "property_id": "PROP-TOKYO-7",
                    "gm_alias": "gm-tanaka",
                },
            ),
            patch("server._dynamodb_resource", mock_dynamodb_resource),
            patch(
                "nova_sonic_session._bedrock_client.invoke_model_with_bidirectional_stream",
                new_callable=AsyncMock,
                return_value=mock_nova_sonic_stream,
            ),
            patch.object(NovaSonicSession, "__init__", tracking_init),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # Verify session was created with the Japanese language from settings
        assert len(created_sessions) == 1
        assert created_sessions[0].context.language == "ja-JP"

        # Verify DynamoDB was queried with the correct gmAlias
        mock_settings_table.get_item.assert_called_once_with(
            Key={"gmAlias": "gm-tanaka"}
        )

    @pytest.mark.asyncio
    async def test_identity_failure_prevents_session_creation(
        self,
        mock_websocket: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed identity resolution prevents session creation.

        When resolve_identity raises IdentityError (invalid token), the handler
        must close the WebSocket with an error and never create a session or
        open a Nova Sonic stream.

        Validates: Requirements 7.1, 7.2
        """
        import server
        from identity_resolver import IdentityError
        from nova_sonic_session import NovaSonicSession

        monkeypatch.setattr(server, "_active_sessions", set())
        monkeypatch.setattr(server, "_shutting_down", False)

        # Configure WebSocket to send an identity message with a bad token
        mock_websocket.receive_text.return_value = json.dumps({
            "type": "identity",
            "accessToken": "expired-or-revoked-token",
        })

        # Track if NovaSonicSession is ever instantiated
        session_created = []

        original_init = NovaSonicSession.__init__

        def tracking_init(
            self_inner: Any,
            property_id: str,
            gm_alias: str,
            language: str,
            ws: Any,
        ) -> None:
            """Track any session creation attempt."""
            session_created.append(True)
            original_init(self_inner, property_id, gm_alias, language, ws)

        with (
            patch(
                "server.resolve_identity",
                new_callable=AsyncMock,
                side_effect=IdentityError(
                    "Access token is invalid or expired",
                    reason="token_invalid",
                ),
            ),
            patch.object(NovaSonicSession, "__init__", tracking_init),
        ):
            await server.websocket_handler(mock_websocket, MagicMock())

        # No session should have been created
        assert len(session_created) == 0

        # Error message should have been sent to the browser
        sent_messages = [
            call[0][0] for call in mock_websocket.send_json.call_args_list
        ]
        error_messages = [m for m in sent_messages if m.get("type") == "error"]
        assert len(error_messages) == 1
        assert error_messages[0]["code"] == "IDENTITY_FAILED"

        # No sessions should be in the active set
        assert len(server._active_sessions) == 0


# ---------------------------------------------------------------------------
# Integration Test: Property-Scoped Tool Dispatch
# Validates: Requirements 7.1, 7.2
# ---------------------------------------------------------------------------


class TestPropertyScopedToolDispatch:
    """Tests verifying property_id from identity flows through to tool execution.

    The NovaSonicSession.execute_tool method receives the property_id from
    session context (set during identity resolution), never from Nova Sonic
    model parameters. This defense-in-depth pattern ensures a compromised
    model cannot access data from other properties.

    Validates: Requirements 7.1, 7.2
    """

    @pytest.mark.asyncio
    async def test_execute_tool_uses_session_property_id(
        self,
        mock_nova_sonic_stream: AsyncMock,
    ) -> None:
        """Tool execution receives property_id from session context, not model.

        When Nova Sonic requests a tool invocation, the NovaSonicSession must
        pass the property_id from its own context (set at session creation from
        identity resolution), never from the tool parameters provided by the model.

        Validates: Requirements 7.1, 7.2
        """
        from nova_sonic_session import NovaSonicSession

        # Create a session with a specific property_id from "identity"
        session_property_id = "PROP-DEFENSE-IN-DEPTH"
        mock_ws = AsyncMock()
        mock_ws.closed = False

        session = NovaSonicSession(
            property_id=session_property_id,
            gm_alias="gm-security-test",
            language="en-US",
            ws=mock_ws,
        )

        # Simulate the stream being active (set after start())
        session.context.is_stream_active = True
        session._stream_response = mock_nova_sonic_stream

        # Mock dispatch_tool to capture the property_id it receives
        dispatched_params: list = []

        async def mock_dispatch(
            tool_name: str, property_id: str, params: Dict[str, Any]
        ) -> Dict[str, Any]:
            """Capture tool dispatch parameters."""
            dispatched_params.append({
                "tool_name": tool_name,
                "property_id": property_id,
                "params": params,
            })
            return {"status": "success", "data": []}

        with patch("nova_sonic_session.dispatch_tool", mock_dispatch):
            # Simulate tool execution - model provides its own "propertyId"
            # in params, but session must use its own context property_id
            model_provided_params = {
                "propertyId": "PROP-ATTACKER-INJECTED",
                "date": "2024-01-15",
            }

            await session.execute_tool(
                tool_name="get_occupancy",
                tool_params=model_provided_params,
                tool_use_id="tool-use-001",
            )

        # dispatch_tool MUST receive the session's property_id, not the model's
        assert len(dispatched_params) == 1
        assert dispatched_params[0]["property_id"] == session_property_id
        assert dispatched_params[0]["property_id"] != "PROP-ATTACKER-INJECTED"
        assert dispatched_params[0]["tool_name"] == "get_occupancy"

        # The model's params are passed separately (handler can use or ignore them)
        assert dispatched_params[0]["params"] == model_provided_params
