"""Exercise the real GA SDK configuration and LUMI's voice protocol adapter.

Only network operations are mocked. Tests cover PCM preservation, cumulative
transcripts, response boundaries, property isolation, and lifecycle cleanup.
"""

import asyncio
import base64
import json
import time
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strands.bidi.types import (
    BidiAudioStartEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
    BidiTranscriptStartEvent,
    BidiUsageEvent,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova_sonic_session import (  # noqa: E402 - service directory is not a package
    MODEL_ID,
    NovaSonicSession,
    PropertyScopedTool,
)
from system_prompt import SYSTEM_PROMPT  # noqa: E402
from tools_config import TOOL_CONFIGURATION  # noqa: E402


@pytest.fixture
def agent():
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    mock.send = AsyncMock()
    return mock


@pytest.fixture
def session(agent):
    with patch("nova_sonic_session.BidiAgent", return_value=agent):
        return NovaSonicSession("PROP-TEST", "gm-test", "en-US", AsyncMock())


def messages(session):
    return [call.args[0] for call in session.context.ws.send_json.call_args_list]


@pytest.mark.parametrize("language", ["en-US", "es-US", "ja-JP", "zh-CN"])
def test_preserves_supported_language(language, agent):
    with patch("nova_sonic_session.BidiAgent", return_value=agent):
        session = NovaSonicSession("PROP", "gm", language, AsyncMock())
    assert session.context.language == language


@pytest.mark.parametrize("language", ["", None, "fr-FR"])
def test_language_falls_back_to_english(language, agent):
    with patch("nova_sonic_session.BidiAgent", return_value=agent):
        session = NovaSonicSession("PROP", "gm", language, AsyncMock())
    assert session.context.language == "en-US"


def test_real_ga_model_preserves_configuration():
    session = NovaSonicSession("PROP", "gm", "en-US", AsyncMock())
    model = session._agent.model
    assert model.get_config()["model_id"] == "amazon.nova-2-5-sonic"
    assert model.get_audio_config() == {
        "input": {"sample_rate": 16000, "channels": 1, "format": "pcm"},
        "output": {"sample_rate": 24000, "channels": 1, "format": "pcm"},
    }
    assert model._voice == "tiffany"
    assert model.get_config()["params"] == {
        "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
        "turnDetectionConfiguration": {"endpointingSensitivity": "MEDIUM"},
    }
    # Renewal remains within Nova's eight-minute connection cap.
    assert model.get_config()["connection"]["restart_after_s"] == 420


@pytest.mark.asyncio
async def test_model_id_reaches_real_ga_streaming_request():
    session = NovaSonicSession("PROP", "gm", "en-US", AsyncMock())
    model = session._agent.model
    credentials = MagicMock(access_key="testing", secret_key="testing", token=None)
    model._session = MagicMock()
    model._session.get_credentials.return_value = credentials
    stream = MagicMock()
    stream.input_stream.send = AsyncMock()
    stream.close = AsyncMock()
    client = MagicMock()
    client.close = AsyncMock()
    client.invoke_model_with_bidirectional_stream = AsyncMock(return_value=stream)
    with patch(
        "strands.bidi.models.bedrock.AsyncBedrockRuntimeClient", return_value=client
    ):
        await model.start(system_prompt=SYSTEM_PROMPT, tools=[])
    request = client.invoke_model_with_bidirectional_stream.call_args.args[0]
    assert request.model_id == MODEL_ID == "amazon.nova-2-5-sonic"
    assert stream.input_stream.send.await_count > 0
    await model.stop()


def test_registers_exact_existing_tools_and_prompt(agent):
    with patch("nova_sonic_session.BidiAgent", return_value=agent) as constructor:
        NovaSonicSession("PROP", "gm", "en-US", AsyncMock())
    config = constructor.call_args.kwargs
    assert config["system_prompt"] == SYSTEM_PROMPT
    assert [tool.tool_name for tool in config["tools"]] == [
        item["toolSpec"]["name"] for item in TOOL_CONFIGURATION
    ]
    for tool, original in zip(config["tools"], TOOL_CONFIGURATION):
        assert tool.tool_spec["description"] == original["toolSpec"]["description"]
        assert tool.tool_spec["inputSchema"]["json"] == json.loads(
            original["toolSpec"]["inputSchema"]["json"]
        )
    # Converting schemas for Strands must not mutate shared configuration.
    assert isinstance(TOOL_CONFIGURATION[0]["toolSpec"]["inputSchema"]["json"], str)


@pytest.mark.asyncio
async def test_start_and_close_are_idempotent(session, agent):
    await session.start()
    assert session.context.is_stream_active
    await asyncio.gather(session.close(), session.close())
    assert not session.context.is_stream_active
    agent.start.assert_awaited_once()
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_provider_close_does_not_prevent_server_cleanup(session, agent):
    await session.start()
    agent.stop.side_effect = RuntimeError("provider teardown failed")
    await session.close()
    await session.close()
    assert not session.context.is_stream_active
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_start_cleans_up_without_marking_ready(session, agent):
    agent.start.side_effect = RuntimeError("failed")
    with pytest.raises(RuntimeError):
        await session.start()
    assert not session.context.is_stream_active
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_start_cleans_up(session, agent):
    agent.start.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await session.start()
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_pcm_samples_are_forwarded_unchanged(session, agent):
    await session.start()
    audio = bytes(range(256)) * 2
    session.context.last_activity = time.time() - 59
    await session.send_audio(base64.b64encode(audio).decode())
    agent.send.assert_awaited_once_with(
        {"audio_delta": {"format": "pcm", "source": {"bytes": audio}}}
    )
    assert not session.is_idle()
    assert session._audio_bytes == len(audio)


@pytest.mark.asyncio
async def test_inactive_session_drops_audio(session, agent):
    await session.send_audio("AAA=")
    agent.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", ["!!!", "AA==", ""])
async def test_invalid_pcm_is_rejected(session, agent, audio):
    await session.start()
    with pytest.raises(ValueError):
        await session.send_audio(audio)
    agent.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_vip_transcript_fragments_accumulate_and_final_replaces(session):
    await session._process_output_event(BidiTranscriptStartEvent("user", "u1"))
    for text in ("Any", " VIP", " arrivals?"):
        await session._process_output_event(
            BidiTranscriptDeltaEvent(text, "user", "u1")
        )
    assert [m["text"] for m in messages(session)] == [
        "Any",
        "Any VIP",
        "Any VIP arrivals?",
    ]
    assert all(not m["isFinal"] for m in messages(session))
    await session._process_output_event(
        BidiTranscriptBlockEvent("Any VIP arrivals?", "user", "u1")
    )
    assert messages(session)[-1] == {
        "type": "userTranscript",
        "text": "Any VIP arrivals?",
        "isFinal": True,
    }
    assert not session._transcripts


@pytest.mark.asyncio
async def test_interleaved_transcripts_keep_roles_and_content_separate(session):
    for role, cid in (("user", "u1"), ("assistant", "a1")):
        await session._process_output_event(BidiTranscriptStartEvent(role, cid))
    for text, role, cid in (
        ("Any VIP", "user", "u1"),
        ("Three", "assistant", "a1"),
        (" arrivals?", "user", "u1"),
        (" arrivals.", "assistant", "a1"),
    ):
        await session._process_output_event(BidiTranscriptDeltaEvent(text, role, cid))
    assert messages(session)[-2]["text"] == "Any VIP arrivals?"
    assert messages(session)[-1]["text"] == "Three arrivals."
    assert messages(session)[-1]["type"] == "agentTranscript"


@pytest.mark.asyncio
async def test_next_user_turn_does_not_append_previous_question(session):
    for cid, text in (("u1", "Any VIP arrivals?"), ("u2", "What time?")):
        await session._process_output_event(BidiTranscriptStartEvent("user", cid))
        await session._process_output_event(BidiTranscriptDeltaEvent(text, "user", cid))
        await session._process_output_event(BidiTranscriptBlockEvent(text, "user", cid))
    assert messages(session)[-1]["text"] == "What time?"


@pytest.mark.asyncio
async def test_response_start_not_repeated_for_audio_and_transcript_blocks(session):
    await session._process_output_event(BidiResponseStartEvent("r1"))
    await session._process_output_event(BidiTranscriptStartEvent("user", "u1"))
    assert not messages(session)
    await session._process_output_event(BidiTranscriptStartEvent("assistant", "a1"))
    await session._process_output_event(BidiAudioStartEvent("audio1"))
    await session._process_output_event({"type": "bidi_audio_delta", "audio": "AAA="})
    # Transcript/audio block stops must not prematurely switch to listening.
    await session._process_output_event(
        {"type": "bidi_audio_stop", "content_id": "audio1"}
    )
    assert messages(session) == [
        {"type": "contentStart", "role": "ASSISTANT"},
        {"type": "audioOutput", "audioData": "AAA="},
    ]
    await session._process_output_event(BidiResponseStopEvent("r1"))
    assert messages(session)[-1] == {"type": "contentEnd"}


@pytest.mark.asyncio
async def test_barge_in_uses_existing_playback_reset_messages(session):
    await session._process_output_event({"type": "bidi_barge_in"})
    assert messages(session) == [
        {"type": "contentStart", "role": "ASSISTANT"},
        {"type": "contentEnd"},
    ]


@pytest.mark.asyncio
async def test_restart_discards_partial_transcripts(session):
    await session._process_output_event(BidiTranscriptStartEvent("user", "u1"))
    await session._process_output_event(
        BidiTranscriptDeltaEvent("partial", "user", "u1")
    )
    await session._process_output_event({"type": "bidi_connection_start"})
    assert not session._transcripts


@pytest.mark.asyncio
async def test_tool_notification_keeps_browser_contract(session):
    await session._process_output_event(
        {
            "type": "bidi_tool_use_blocks",
            "tool_uses": [{"name": "get_vip_guests", "toolUseId": "t1", "input": {}}],
        }
    )
    assert messages(session) == [{"type": "toolUse", "toolName": "get_vip_guests"}]


@pytest.mark.asyncio
async def test_property_scope_cannot_be_overridden_by_model_or_invocation_state():
    spec = TOOL_CONFIGURATION[2]["toolSpec"]
    tool = PropertyScopedTool(spec, "PROP-AUTHENTICATED")
    dispatch = AsyncMock(return_value={"status": "success", "data": []})
    with patch("nova_sonic_session.dispatch_tool", dispatch):
        results = [
            result
            async for result in tool.stream(
                {
                    "name": "get_vip_guests",
                    "toolUseId": "t1",
                    "input": {
                        "propertyId": "OTHER",
                        "property_id": "OTHER",
                        "date": "2026-10-07",
                    },
                },
                {"property_id": "OTHER"},
            )
        ]
    dispatch.assert_awaited_once_with(
        tool_name="get_vip_guests",
        property_id="PROP-AUTHENTICATED",
        params={"date": "2026-10-07"},
    )
    assert results == [
        {
            "toolUseId": "t1",
            "status": "success",
            "content": [{"json": {"status": "success", "data": []}}],
        }
    ]


@pytest.mark.asyncio
async def test_parallel_sessions_do_not_share_property_scope():
    tools = [
        PropertyScopedTool(TOOL_CONFIGURATION[0]["toolSpec"], pid)
        for pid in ("PROP-A", "PROP-B")
    ]
    dispatch = AsyncMock(return_value={"status": "success", "data": {}})

    async def run(tool):
        return [r async for r in tool.stream({"toolUseId": "t", "input": {}}, {})]

    with patch("nova_sonic_session.dispatch_tool", dispatch):
        await asyncio.gather(*(run(tool) for tool in tools))
    assert {call.kwargs["property_id"] for call in dispatch.call_args_list} == {
        "PROP-A",
        "PROP-B",
    }


@pytest.mark.asyncio
async def test_tool_unavailability_is_preserved_for_model():
    tool = PropertyScopedTool(TOOL_CONFIGURATION[2]["toolSpec"], "PROP")
    unavailable = {
        "status": "unavailable",
        "message": "I don't have that data right now.",
    }
    with patch("nova_sonic_session.dispatch_tool", AsyncMock(return_value=unavailable)):
        results = [r async for r in tool.stream({"toolUseId": "t1", "input": {}}, {})]
    assert results[0]["content"] == [{"json": unavailable}]


@pytest.mark.asyncio
async def test_receive_failure_closes_socket_and_agent(session, agent):
    async def receive():
        raise RuntimeError("sensitive provider body")
        yield {}

    agent.receive = receive
    await session.start()
    await session.handle_output_events()
    assert messages(session)[-1]["code"] == "STREAM_DISCONNECTED"
    assert "sensitive" not in str(messages(session))
    session.context.ws.close.assert_awaited_once_with(code=1011)
    agent.stop.assert_awaited_once()
    assert not session.context.is_stream_active


@pytest.mark.asyncio
async def test_usage_event_does_not_interrupt_transcription(session, agent):
    """Real GA usage envelopes precede speech and use camel-case token keys."""

    async def receive():
        yield BidiUsageEvent(input_tokens=20, output_tokens=3, total_tokens=23)
        yield BidiTranscriptBlockEvent("Any VIP arrivals?", "user", "u1")

    agent.receive = receive
    await session.start()
    await session.handle_output_events()
    assert messages(session) == [
        {"type": "userTranscript", "text": "Any VIP arrivals?", "isFinal": True}
    ]
    session.context.ws.close.assert_not_awaited()
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_receive_exhaustion_stops_agent(session, agent):
    async def receive():
        yield BidiTranscriptBlockEvent("Any VIP arrivals?", "user", "u1")

    agent.receive = receive
    await session.start()
    await session.handle_output_events()
    agent.stop.assert_awaited_once()
    assert not session.context.is_stream_active


def test_idle_boundary(session):
    session.context.last_activity = time.time() - 61
    assert session.is_idle()
    session.reset_idle_timer()
    assert not session.is_idle()


@pytest.mark.asyncio
async def test_real_ga_agent_assembles_history_and_executes_scoped_tool():
    """Exercise the real agent loop, not a mocked agent, over two user turns."""
    from strands.bidi.agent import BidiAgent
    from strands.bidi.models import BidiModel
    from strands.types.tools import ToolResultBlock
    from strands.bidi.types import (
        BidiConnectionStartEvent,
        BidiToolUseBlocksEvent,
        BidiTranscriptStopEvent,
    )

    model = MagicMock(spec=BidiModel)
    model.get_config.return_value = {"model_id": MODEL_ID}
    model.get_connection_config.return_value = {}
    model.start = AsyncMock()
    model.stop = AsyncMock()
    model.send = AsyncMock()
    queue = asyncio.Queue()
    events = [BidiConnectionStartEvent("connection1", MODEL_ID)]
    for number, phrase in enumerate(("Any VIP arrivals?", "What time?")):
        cid = f"u{number}"
        events.extend(
            [
                BidiTranscriptStartEvent("user", cid),
                BidiTranscriptDeltaEvent(phrase, "user", cid),
                BidiTranscriptStopEvent("user", cid),
            ]
        )
    events.extend(
        [
            BidiResponseStartEvent("r1"),
            BidiToolUseBlocksEvent(
                [{"name": "get_vip_guests", "toolUseId": "t1", "input": {}}]
            ),
            BidiResponseStopEvent("r1"),
        ]
    )
    for event in events:
        queue.put_nowait(event)

    async def receive():
        while True:
            yield await queue.get()

    model.receive = receive
    tool = PropertyScopedTool(TOOL_CONFIGURATION[2]["toolSpec"], "PROP-AUTHENTICATED")
    agent = BidiAgent(model=model, tools=[tool], system_prompt=SYSTEM_PROMPT)
    result = {"status": "success", "data": {"count": 3}}

    async def send(content):
        if any(
            isinstance(block, ToolResultBlock) and block.content == [{"json": result}]
            for block in content.content
        ):
            # A second response can follow only after the result reaches the model.
            queue.put_nowait(BidiResponseStartEvent("r2"))
            queue.put_nowait(BidiResponseStopEvent("r2"))

    model.send.side_effect = send
    dispatch = AsyncMock(return_value=result)
    completed = []
    with patch("nova_sonic_session.dispatch_tool", dispatch):
        await agent.start()
        try:
            async with asyncio.timeout(2):
                async for event in agent.receive():
                    completed.append(event)
                    if (
                        event.get("type") == "bidi_response_stop"
                        and event["response_id"] == "r2"
                    ):
                        break
        finally:
            await agent.stop()
    assert [
        event["transcript"]
        for event in completed
        if event.get("type") == "bidi_transcript_block"
    ] == ["Any VIP arrivals?", "What time?"]
    dispatch.assert_awaited_once_with(
        tool_name="get_vip_guests", property_id="PROP-AUTHENTICATED", params={}
    )
    history = [
        block["text"]
        for message in agent.messages
        for block in message["content"]
        if "text" in block
    ]
    assert "Any VIP arrivals?" in history and "What time?" in history
    assert any(
        isinstance(block, ToolResultBlock) and block.content == [{"json": result}]
        for call in model.send.call_args_list
        for block in call.args[0].content
    )
