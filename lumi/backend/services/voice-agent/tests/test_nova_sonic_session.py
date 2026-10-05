"""Unit tests for NovaSonicSession (nova_sonic_session.py).

Tests validate audio forwarding, output event relaying, tool result sending,
language resolution, and idle timeout detection for the Nova Sonic bidirectional
stream session manager. Uses mocked Bedrock stream client and WebSocket.

Validates: Requirements 1.3, 1.4, 3.3, 4.1, 4.4
"""

import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Add the voice-agent service directory to the path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Mock external SDK modules that are not installed in the test environment.
# The aws_sdk_bedrock_runtime and smithy_aws_core packages are experimental
# and only available in the container. We mock them so nova_sonic_session
# can be imported without those dependencies.
# ---------------------------------------------------------------------------

_mock_bedrock_client_module = ModuleType("aws_sdk_bedrock_runtime.client")
_mock_bedrock_client_module.BedrockRuntimeClient = MagicMock
_mock_bedrock_client_module.InvokeModelWithBidirectionalStreamOperationInput = MagicMock

_mock_bedrock_config_module = ModuleType("aws_sdk_bedrock_runtime.config")
_mock_bedrock_config_module.Config = MagicMock

_mock_bedrock_models_module = ModuleType("aws_sdk_bedrock_runtime.models")
_mock_bedrock_models_module.BidirectionalInputPayloadPart = MagicMock
_mock_bedrock_models_module.InvokeModelWithBidirectionalStreamInputChunk = MagicMock

_mock_smithy_module = ModuleType("smithy_aws_core")
_mock_smithy_identity_module = ModuleType("smithy_aws_core.identity")
_mock_smithy_env_module = ModuleType("smithy_aws_core.identity.environment")
_mock_smithy_env_module.EnvironmentCredentialsResolver = MagicMock

sys.modules.setdefault("aws_sdk_bedrock_runtime", ModuleType("aws_sdk_bedrock_runtime"))
sys.modules.setdefault("aws_sdk_bedrock_runtime.client", _mock_bedrock_client_module)
sys.modules.setdefault("aws_sdk_bedrock_runtime.config", _mock_bedrock_config_module)
sys.modules.setdefault("aws_sdk_bedrock_runtime.models", _mock_bedrock_models_module)
sys.modules.setdefault("smithy_aws_core", _mock_smithy_module)
sys.modules.setdefault("smithy_aws_core.identity", _mock_smithy_identity_module)
sys.modules.setdefault("smithy_aws_core.identity.environment", _mock_smithy_env_module)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables expected by nova_sonic_session at import time.

    Table names are needed by tool_handlers (imported by nova_sonic_session).
    AWS_DEFAULT_REGION is used for Bedrock client configuration.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", "stayos-reservations")
    monkeypatch.setenv("ROOMS_TABLE_NAME", "stayos-rooms")
    monkeypatch.setenv("GUESTS_TABLE_NAME", "stayos-guests")
    monkeypatch.setenv("REVENUES_TABLE_NAME", "stayos-revenues")
    monkeypatch.setenv("WORK_ORDERS_TABLE_NAME", "stayos-work-orders")


@pytest.fixture()
def mock_ws() -> AsyncMock:
    """Create a mock aiohttp WebSocketResponse for capturing sent messages.

    The mock records all send_json calls so tests can inspect messages
    that would have been sent to the browser.

    Returns:
        AsyncMock representing the WebSocket connection.
    """
    ws = AsyncMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture()
def mock_stream_response() -> MagicMock:
    """Create a mock Bedrock bidirectional stream response.

    Provides input_stream.send as an AsyncMock so tests can capture
    events that would have been sent to Nova Sonic.

    Returns:
        MagicMock representing the stream response object.
    """
    stream_response = MagicMock()
    stream_response.input_stream = MagicMock()
    stream_response.input_stream.send = AsyncMock()
    stream_response.input_stream.close = AsyncMock()
    return stream_response


@pytest.fixture()
def mock_bedrock_client(
    monkeypatch: pytest.MonkeyPatch,
    mock_stream_response: MagicMock,
) -> AsyncMock:
    """Patch the module-level _bedrock_client to return a mock stream.

    Monkeypatches the global Bedrock client so NovaSonicSession.start()
    does not make real AWS API calls.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        mock_stream_response: The mock stream response returned by the client.

    Returns:
        The mock client with invoke_model_with_bidirectional_stream configured.
    """
    import nova_sonic_session

    mock_client = AsyncMock()
    mock_client.invoke_model_with_bidirectional_stream = AsyncMock(
        return_value=mock_stream_response
    )
    monkeypatch.setattr(nova_sonic_session, "_bedrock_client", mock_client)
    return mock_client


@pytest.fixture()
def session(
    mock_ws: AsyncMock,
    mock_bedrock_client: AsyncMock,
    mock_stream_response: MagicMock,
) -> "NovaSonicSession":
    """Create a NovaSonicSession instance with mocked dependencies.

    The session is pre-configured with a property_id, gm_alias, and English
    language. The stream is not yet started (call session.start() in tests
    that need an active stream).

    Returns:
        A NovaSonicSession ready for testing.
    """
    from nova_sonic_session import NovaSonicSession

    return NovaSonicSession(
        property_id="PROP-TEST-001",
        gm_alias="gm-sarah",
        language="en-US",
        ws=mock_ws,
    )


@pytest_asyncio.fixture()
async def started_session(
    session: "NovaSonicSession",
    mock_stream_response: MagicMock,
) -> "NovaSonicSession":
    """Create a NovaSonicSession that has already called start().

    Resets the send mock after start() so tests only see events
    from the method under test (not setup events).

    Returns:
        A NovaSonicSession with an active stream.
    """
    await session.start()
    # Clear events sent during start() so tests only see new ones
    mock_stream_response.input_stream.send.reset_mock()
    return session


# ---------------------------------------------------------------------------
# Property 8: Language Resolution
# Verify supported languages pass through, invalid defaults to en-US.
# ---------------------------------------------------------------------------


class TestLanguageResolution:
    """Tests verifying language resolution at session construction time.

    **Validates: Requirement 4.1, 4.4**
    """

    def test_language_resolution_supported_en_us(self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock) -> None:
        """en-US passes through as a supported language.

        Property 8: Supported languages pass through unchanged.
        Validates: Requirement 4.1
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "en-US", mock_ws)

        assert session.context.language == "en-US"

    def test_language_resolution_supported_es_us(self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock) -> None:
        """es-US passes through as a supported language.

        Property 8: Supported languages pass through unchanged.
        Validates: Requirement 4.1
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "es-US", mock_ws)

        assert session.context.language == "es-US"

    def test_language_resolution_supported_ja_jp(self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock) -> None:
        """ja-JP passes through as a supported language.

        Property 8: Supported languages pass through unchanged.
        Validates: Requirement 4.1
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "ja-JP", mock_ws)

        assert session.context.language == "ja-JP"

    def test_language_resolution_supported_zh_cn(self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock) -> None:
        """zh-CN passes through as a supported language.

        Property 8: Supported languages pass through unchanged.
        Validates: Requirement 4.1
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "zh-CN", mock_ws)

        assert session.context.language == "zh-CN"

    def test_language_resolution_unsupported_defaults_to_english_fr(
        self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock
    ) -> None:
        """fr-FR (unsupported) defaults to en-US.

        Property 8: Invalid languages default to en-US.
        Validates: Requirement 4.4
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "fr-FR", mock_ws)

        assert session.context.language == "en-US"

    def test_language_resolution_unsupported_defaults_to_english_empty(
        self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock
    ) -> None:
        """Empty string defaults to en-US.

        Property 8: Invalid languages default to en-US.
        Validates: Requirement 4.4
        """
        from nova_sonic_session import NovaSonicSession

        session = NovaSonicSession("PROP-001", "gm-test", "", mock_ws)

        assert session.context.language == "en-US"

    def test_language_resolution_unsupported_defaults_to_english_none(
        self, mock_ws: AsyncMock, mock_bedrock_client: AsyncMock
    ) -> None:
        """None (cast scenario) defaults to en-US.

        Property 8: Invalid languages default to en-US.
        Validates: Requirement 4.4
        """
        from nova_sonic_session import NovaSonicSession

        # None is not in SUPPORTED_LANGUAGES, so fallback triggers
        session = NovaSonicSession("PROP-001", "gm-test", None, mock_ws)

        assert session.context.language == "en-US"


# ---------------------------------------------------------------------------
# Property 1: Audio Input Forwarding Preserves Content
# Verify base64 audio received on WS produces identical audioInput event on stream.
# ---------------------------------------------------------------------------


class TestAudioInputForwarding:
    """Tests verifying audio chunks are forwarded to Nova Sonic without modification.

    **Validates: Requirement 1.3**
    """

    @pytest.mark.asyncio
    async def test_send_audio_produces_audio_input_event(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
    ) -> None:
        """Sending audio produces an audioInput event on the stream.

        Property 1: Base64 audio received on WS produces audioInput event.
        Validates: Requirement 1.3
        """
        test_audio = "SGVsbG8gV29ybGQ="  # base64 "Hello World"

        await started_session.send_audio(test_audio)

        # Verify at least one send call was made
        assert mock_stream_response.input_stream.send.called

        # Find the audioInput event in the sent chunks
        sent_events = _extract_sent_events(mock_stream_response)
        audio_events = [e for e in sent_events if "audioInput" in e.get("event", {})]

        assert len(audio_events) == 1, "Expected exactly one audioInput event"
        assert audio_events[0]["event"]["audioInput"]["content"] == test_audio

    @pytest.mark.asyncio
    async def test_send_audio_content_preserved(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
    ) -> None:
        """The exact base64 content is forwarded without modification.

        Property 1: Audio content is forwarded byte-for-byte.
        Validates: Requirement 1.3
        """
        # Use a longer, realistic-looking base64 audio payload
        test_audio = "AAAAAAAAAAAAAAAA/f39/f39/f0BAQEBAQEBAQE="

        await started_session.send_audio(test_audio)

        sent_events = _extract_sent_events(mock_stream_response)
        audio_events = [e for e in sent_events if "audioInput" in e.get("event", {})]

        assert len(audio_events) == 1
        forwarded_content = audio_events[0]["event"]["audioInput"]["content"]
        assert forwarded_content == test_audio, (
            "Audio content must be forwarded exactly as received, no re-encoding"
        )

    @pytest.mark.asyncio
    async def test_send_audio_on_inactive_stream_is_ignored(
        self,
        session: "NovaSonicSession",
        mock_stream_response: MagicMock,
    ) -> None:
        """Audio sent when stream is inactive does not produce events.

        The session has not called start(), so is_stream_active is False.
        send_audio should return without sending.
        Validates: Requirement 1.3 (boundary case)
        """
        await session.send_audio("dGVzdA==")

        # No events should be sent since the stream is not active
        mock_stream_response.input_stream.send.assert_not_called()


# ---------------------------------------------------------------------------
# Property 2: Nova Sonic Output Events Are Relayed Correctly
# Verify audioOutput/textOutput/toolUse events produce correct WS messages.
# ---------------------------------------------------------------------------


class TestOutputEventRelaying:
    """Tests verifying Nova Sonic output events are correctly relayed to the browser.

    **Validates: Requirements 1.4, 4.1**
    """

    @pytest.mark.asyncio
    async def test_handle_audio_output_relays_to_ws(
        self,
        started_session: "NovaSonicSession",
        mock_ws: AsyncMock,
    ) -> None:
        """audioOutput event from Nova Sonic is sent to WS as audioOutput message.

        Property 2: audioOutput events produce correct WebSocket messages.
        Validates: Requirement 1.4
        """
        audio_data = "base64EncodedAudioChunkData=="

        # Simulate processing an audioOutput event from Nova Sonic
        event_data = {
            "event": {
                "audioOutput": {
                    "content": audio_data,
                }
            }
        }
        await started_session._process_output_event(event_data)

        # Verify the WebSocket received the correct message
        mock_ws.send_json.assert_called_once_with({
            "type": "audioOutput",
            "audioData": audio_data,
        })

    @pytest.mark.asyncio
    async def test_handle_text_output_user_relays_as_user_transcript(
        self,
        started_session: "NovaSonicSession",
        mock_ws: AsyncMock,
    ) -> None:
        """textOutput with role USER relays as userTranscript on WebSocket.

        Property 2: textOutput events with USER role produce userTranscript.
        Validates: Requirement 1.4
        """
        event_data = {
            "event": {
                "textOutput": {
                    "role": "USER",
                    "content": "What is my occupancy today?",
                }
            }
        }
        await started_session._process_output_event(event_data)

        mock_ws.send_json.assert_called_once_with({
            "type": "userTranscript",
            "text": "What is my occupancy today?",
            "isFinal": True,
        })

    @pytest.mark.asyncio
    async def test_handle_text_output_assistant_relays_as_agent_transcript(
        self,
        started_session: "NovaSonicSession",
        mock_ws: AsyncMock,
    ) -> None:
        """textOutput with role ASSISTANT relays as agentTranscript on WebSocket.

        Property 2: textOutput events with ASSISTANT role produce agentTranscript.
        Validates: Requirement 1.4
        """
        event_data = {
            "event": {
                "textOutput": {
                    "role": "ASSISTANT",
                    "content": "Your occupancy is 92% today.",
                }
            }
        }
        await started_session._process_output_event(event_data)

        mock_ws.send_json.assert_called_once_with({
            "type": "agentTranscript",
            "text": "Your occupancy is 92% today.",
            "isFinal": True,
        })

    @pytest.mark.asyncio
    async def test_handle_content_start_relays_to_ws(
        self,
        started_session: "NovaSonicSession",
        mock_ws: AsyncMock,
    ) -> None:
        """contentStart with ASSISTANT role relays contentStart to WebSocket.

        Property 2: contentStart events produce correct WS messages for UI state.
        Validates: Requirement 1.4
        """
        event_data = {
            "event": {
                "contentStart": {
                    "role": "ASSISTANT",
                }
            }
        }
        await started_session._process_output_event(event_data)

        mock_ws.send_json.assert_called_once_with({
            "type": "contentStart",
            "role": "ASSISTANT",
        })

    @pytest.mark.asyncio
    async def test_handle_content_start_non_assistant_not_relayed(
        self,
        started_session: "NovaSonicSession",
        mock_ws: AsyncMock,
    ) -> None:
        """contentStart with non-ASSISTANT role does not relay to WebSocket.

        Only ASSISTANT contentStart events are relevant for UI state transitions.
        Validates: Requirement 1.4 (boundary case)
        """
        event_data = {
            "event": {
                "contentStart": {
                    "role": "USER",
                }
            }
        }
        await started_session._process_output_event(event_data)

        mock_ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# Property 6: Tool Results Are Forwarded as Valid Events
# Verify any tool handler return produces valid toolResult event.
# ---------------------------------------------------------------------------


class TestToolResultForwarding:
    """Tests verifying tool execution results are sent as valid toolResult events.

    **Validates: Requirement 3.3**
    """

    @pytest.mark.asyncio
    async def test_execute_tool_sends_tool_result_to_stream(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_tool dispatches and sends toolResult event to the stream.

        Property 6: Any tool handler return produces valid toolResult event.
        Validates: Requirement 3.3
        """
        import nova_sonic_session

        # Mock dispatch_tool to return a success result
        mock_dispatch = AsyncMock(return_value={
            "status": "success",
            "data": {"occupancyPct": 85, "arrivalsTotal": 12},
        })
        monkeypatch.setattr(nova_sonic_session, "dispatch_tool", mock_dispatch)

        await started_session.execute_tool(
            tool_name="getOccupancyTool",
            tool_params={"date": "2025-01-15"},
            tool_use_id="tool-use-abc123",
        )

        # Extract all events sent to the stream after execute_tool
        sent_events = _extract_sent_events(mock_stream_response)

        # Should have: contentStart (TOOL_RESULT), toolResult, contentEnd
        tool_result_events = [
            e for e in sent_events
            if "toolResult" in e.get("event", {})
        ]
        assert len(tool_result_events) == 1, "Expected exactly one toolResult event"

        # Verify toolResult structure
        tool_result = tool_result_events[0]["event"]["toolResult"]
        assert tool_result["promptName"] == started_session._prompt_name
        result_content = json.loads(tool_result["content"])
        assert result_content["status"] == "success"
        assert result_content["data"]["occupancyPct"] == 85

    @pytest.mark.asyncio
    async def test_execute_tool_sends_content_start_with_tool_use_id(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_tool sends contentStart with TOOL_RESULT type and toolUseId.

        Property 6: Tool result event includes the correlation toolUseId.
        Validates: Requirement 3.3
        """
        import nova_sonic_session

        mock_dispatch = AsyncMock(return_value={
            "status": "success",
            "data": {"adr": 250.00},
        })
        monkeypatch.setattr(nova_sonic_session, "dispatch_tool", mock_dispatch)

        await started_session.execute_tool(
            tool_name="getRevenueTool",
            tool_params={},
            tool_use_id="tool-use-xyz789",
        )

        sent_events = _extract_sent_events(mock_stream_response)

        # Find the contentStart for TOOL result
        content_starts = [
            e for e in sent_events
            if "contentStart" in e.get("event", {})
            and e["event"]["contentStart"].get("type") == "TOOL"
        ]
        assert len(content_starts) == 1

        content_start = content_starts[0]["event"]["contentStart"]
        assert content_start["role"] == "TOOL"
        tool_config = content_start["toolResultInputConfiguration"]
        assert tool_config["toolUseId"] == "tool-use-xyz789"

    @pytest.mark.asyncio
    async def test_execute_tool_uses_session_property_id(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_tool calls dispatch_tool with the session's property_id.

        Property 6: Tool dispatch always uses session property_id for scope.
        Validates: Requirement 3.3
        """
        import nova_sonic_session

        mock_dispatch = AsyncMock(return_value={"status": "success", "data": {}})
        monkeypatch.setattr(nova_sonic_session, "dispatch_tool", mock_dispatch)

        await started_session.execute_tool(
            tool_name="getRoomStatusTool",
            tool_params={},
            tool_use_id="tool-use-001",
        )

        # Verify dispatch_tool was called with the session's property_id
        mock_dispatch.assert_called_once_with(
            tool_name="getRoomStatusTool",
            property_id="PROP-TEST-001",
            params={},
        )

    @pytest.mark.asyncio
    async def test_execute_tool_unavailable_result_forwarded(
        self,
        started_session: "NovaSonicSession",
        mock_stream_response: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unavailability result from tool handler is forwarded as toolResult.

        Property 6: Any tool handler return (including unavailability) produces
        a valid toolResult event so Nova Sonic can tell the GM about the gap.
        Validates: Requirement 3.3
        """
        import nova_sonic_session

        mock_dispatch = AsyncMock(return_value={
            "status": "unavailable",
            "message": "Revenue data is temporarily unavailable",
        })
        monkeypatch.setattr(nova_sonic_session, "dispatch_tool", mock_dispatch)

        result = await started_session.execute_tool(
            tool_name="getRevenueTool",
            tool_params={"startDate": "2025-01-15"},
            tool_use_id="tool-use-fail",
        )

        # Verify the result is returned to the caller
        assert result["status"] == "unavailable"

        # Verify it was also sent to the stream
        sent_events = _extract_sent_events(mock_stream_response)
        tool_result_events = [
            e for e in sent_events
            if "toolResult" in e.get("event", {})
        ]
        assert len(tool_result_events) == 1
        content = json.loads(tool_result_events[0]["event"]["toolResult"]["content"])
        assert content["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Idle Timer and Timeout Detection
# ---------------------------------------------------------------------------


class TestIdleTimeout:
    """Tests verifying idle timer reset and timeout detection.

    **Validates: Requirement 1.3 (send_audio resets timer)**
    """

    def test_reset_idle_timer_updates_last_activity(
        self,
        session: "NovaSonicSession",
    ) -> None:
        """reset_idle_timer updates the last_activity timestamp.

        Validates: Requirement 1.3 (idle timeout tracking)
        """
        old_activity = session.context.last_activity

        # Slight delay to ensure time advances
        time.sleep(0.01)
        session.reset_idle_timer()

        assert session.context.last_activity > old_activity

    def test_is_idle_returns_true_after_timeout(
        self,
        session: "NovaSonicSession",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """is_idle returns True when last_activity exceeds the timeout threshold.

        Validates: Requirement 1.3 (60s idle timeout)
        """
        # Set last_activity to 61 seconds ago
        session.context.last_activity = time.time() - 61

        assert session.is_idle() is True

    def test_is_idle_returns_false_before_timeout(
        self,
        session: "NovaSonicSession",
    ) -> None:
        """is_idle returns False when session is still within timeout window.

        Validates: Requirement 1.3 (60s idle timeout)
        """
        # Fresh session — last_activity was just set
        assert session.is_idle() is False

    @pytest.mark.asyncio
    async def test_send_audio_resets_idle_timer(
        self,
        started_session: "NovaSonicSession",
    ) -> None:
        """send_audio resets the idle timer on every call.

        Property 1 + idle: Audio input prevents idle timeout.
        Validates: Requirement 1.3
        """
        # Artificially set last_activity to a stale time
        started_session.context.last_activity = time.time() - 55

        await started_session.send_audio("dGVzdA==")

        # After send_audio, should no longer be near timeout
        assert started_session.is_idle() is False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_sent_events(mock_stream_response: MagicMock) -> list:
    """Extract all parsed event dicts from the mock stream send calls.

    Iterates over all calls to input_stream.send(), decodes the byte payload
    from each InvokeModelWithBidirectionalStreamInputChunk, and parses the JSON.

    Args:
        mock_stream_response: The mock stream response with recorded send calls.

    Returns:
        List of parsed event dicts sent to the stream.
    """
    events = []
    for call in mock_stream_response.input_stream.send.call_args_list:
        chunk = call[0][0]  # First positional argument
        # The chunk has .value.bytes_ containing the JSON payload
        payload_bytes = chunk.value.bytes_
        event_dict = json.loads(payload_bytes.decode("utf-8"))
        events.append(event_dict)
    return events
