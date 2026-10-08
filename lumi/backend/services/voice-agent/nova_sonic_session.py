"""Adapt a Strands GA BidiAgent session to LUMI's existing browser protocol.

Strands owns the Nova Sonic connection, conversation history, tool execution,
and provider event ordering. This adapter retains authentication-derived
property scope, browser messages, PCM formats, and the server lifecycle.
Application logs contain operational metadata, never transcripts or tool data.
"""

import asyncio
import base64
import copy
import dataclasses
import json
import os
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger
from strands.bidi.agent import BidiAgent
from strands.bidi.models import BedrockNovaSonicModel
from strands.types.tools import AgentTool

from system_prompt import SYSTEM_PROMPT
from tool_handlers import dispatch_tool
from tools_config import TOOL_CONFIGURATION

logger = Logger(service="stayos-voice-agent")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
MODEL_ID = "amazon.nova-2-5-sonic"
SUPPORTED_LANGUAGES = {"en-US", "es-US", "ja-JP", "zh-CN"}
DEFAULT_LANGUAGE = "en-US"
MAX_TOKENS = 1024
TOP_P = 0.9
TEMPERATURE = 0.7
VOICE_ID = "tiffany"
IDLE_TIMEOUT_SECONDS = 60
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000


@dataclasses.dataclass
class SessionContext:
    """Identity and lifecycle state for one authenticated WebSocket session."""

    connection_id: str
    property_id: str
    gm_alias: str
    language: str
    last_activity: float
    is_stream_active: bool
    ws: Any


class PropertyScopedTool(AgentTool):
    """Expose an existing read-only handler without model-controlled scope."""

    def __init__(self, spec: dict[str, Any], property_id: str) -> None:
        super().__init__()
        self._spec = copy.deepcopy(spec)
        # Strands accepts a schema object and serializes the Nova wire schema.
        self._spec["inputSchema"]["json"] = json.loads(
            self._spec["inputSchema"]["json"]
        )
        self._property_id = property_id

    @property
    def tool_name(self) -> str:
        return self._spec["name"]

    @property
    def tool_spec(self) -> dict[str, Any]:
        return self._spec

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(
        self, tool_use: dict[str, Any], invocation_state: dict[str, Any], **kwargs: Any
    ):
        """Return the existing handler result in Strands' tool-result envelope."""
        # Only declared parameters reach the handler, even for malformed calls.
        allowed = self._spec["inputSchema"]["json"]["properties"]
        params = {
            key: value
            for key, value in tool_use.get("input", {}).items()
            if key in allowed
        }
        logger.info("Executing voice tool", tool_name=self.tool_name)
        result = await dispatch_tool(
            tool_name=self.tool_name,
            property_id=self._property_id,
            params=params,
        )
        logger.info(
            "Voice tool completed",
            tool_name=self.tool_name,
            status=result.get("status"),
        )
        yield {
            "toolUseId": tool_use["toolUseId"],
            "status": "success",
            "content": [{"json": result}],
        }


class NovaSonicSession:
    """Preserve the server session interface while Strands manages streaming."""

    def __init__(self, property_id: str, gm_alias: str, language: str, ws: Any) -> None:
        self.context = SessionContext(
            connection_id=str(uuid.uuid4()),
            property_id=property_id,
            gm_alias=gm_alias,
            language=language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE,
            last_activity=time.time(),
            is_stream_active=False,
            ws=ws,
        )
        model = BedrockNovaSonicModel(
            model_id=MODEL_ID,
            region=AWS_REGION,
            voice=VOICE_ID,
            audio={
                "input": {"sample_rate": INPUT_SAMPLE_RATE},
                "output": {"sample_rate": OUTPUT_SAMPLE_RATE},
            },
            params={
                "inferenceConfiguration": {
                    "maxTokens": MAX_TOKENS,
                    "topP": TOP_P,
                    "temperature": TEMPERATURE,
                },
                "turnDetectionConfiguration": {"endpointingSensitivity": "MEDIUM"},
            },
        )
        self._agent = BidiAgent(
            model=model,
            name="LUMI voice",
            system_prompt=SYSTEM_PROMPT,
            tools=[
                PropertyScopedTool(item["toolSpec"], property_id)
                for item in TOOL_CONFIGURATION
            ],
        )
        self._agent_started = False
        self._close_lock = asyncio.Lock()
        self._transcripts: dict[tuple[str, str], str] = {}
        self._response_notified = False
        self._audio_bytes = 0
        self._audio_started_at: float | None = None

    async def start(self) -> None:
        """Open the GA agent before the server confirms session readiness."""
        logger.info(
            "Starting Strands bidirectional voice session",
            connection_id=self.context.connection_id,
            model_id=MODEL_ID,
        )
        try:
            await asyncio.wait_for(self._agent.start(), timeout=10.0)
        except (Exception, asyncio.CancelledError):
            # Startup can allocate provider tasks before failing.
            await self._agent.stop()
            raise
        self._agent_started = True
        self.context.is_stream_active = True

    async def send_audio(self, audio_base64: str) -> None:
        """Pass unchanged 16-bit mono PCM samples to Strands' audio input."""
        if not self.context.is_stream_active:
            return
        audio = base64.b64decode(audio_base64, validate=True)
        if not audio or len(audio) % 2:
            raise ValueError("Voice input must contain complete 16-bit PCM samples")
        if self._audio_started_at is None:
            self._audio_started_at = time.monotonic()
        await self._agent.send(
            {"audio_delta": {"format": "pcm", "source": {"bytes": audio}}}
        )
        self._audio_bytes += len(audio)
        self.reset_idle_timer()

    async def handle_output_events(self) -> None:
        """Relay GA events while Strands runs tools and preserves conversation."""
        try:
            async for event in self._agent.receive():
                await self._process_output_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Exception bodies can include model content; retain only the type.
            logger.error(
                "Voice output stream failed",
                connection_id=self.context.connection_id,
                model_id=MODEL_ID,
                error_type=type(error).__name__,
            )
            await self._send_ws_message(
                {
                    "type": "error",
                    "code": "STREAM_DISCONNECTED",
                    "message": "The voice session was interrupted. Please try again.",
                }
            )
            await self.context.ws.close(code=1011)
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop provider and tool tasks once, including after output failure."""
        async with self._close_lock:
            self.context.is_stream_active = False
            if not self._agent_started:
                return
            self._agent_started = False
            try:
                await self._agent.stop()
            except Exception as error:
                # Strands tries all teardown steps before raising. Keep server
                # cleanup running so sockets/tasks and HealthyBusy state clear.
                logger.warning(
                    "Voice provider teardown failed",
                    connection_id=self.context.connection_id,
                    error_type=type(error).__name__,
                )
            finally:
                self._transcripts.clear()
                logger.info(
                    "Strands voice session closed",
                    connection_id=self.context.connection_id,
                    model_id=MODEL_ID,
                    input_audio_seconds=self._audio_bytes / (INPUT_SAMPLE_RATE * 2),
                    input_elapsed_seconds=(
                        time.monotonic() - self._audio_started_at
                        if self._audio_started_at is not None
                        else 0
                    ),
                )

    def reset_idle_timer(self) -> None:
        self.context.last_activity = time.time()

    def is_idle(self) -> bool:
        return time.time() - self.context.last_activity > IDLE_TIMEOUT_SECONDS

    async def _notify_response(self) -> None:
        """Start the browser response once, rather than once per content block."""
        if not self._response_notified:
            self._response_notified = True
            await self._send_ws_message({"type": "contentStart", "role": "ASSISTANT"})

    async def _process_output_event(self, event: dict[str, Any]) -> None:
        """Translate GA events without exposing Strands internals to the browser."""
        event_type = event.get("type")
        if event_type == "bidi_connection_start":
            self._transcripts.clear()
            self._response_notified = False
            logger.info(
                "Strands model connection ready",
                connection_id=self.context.connection_id,
                model_id=MODEL_ID,
            )
        elif event_type == "bidi_response_start":
            self._response_notified = False
        elif event_type == "bidi_transcript_start":
            key = (event["role"], event["content_id"])
            self._transcripts[key] = ""
            if event["role"] == "assistant":
                await self._notify_response()
        elif event_type == "bidi_transcript_delta":
            key = (event["role"], event["content_id"])
            self._transcripts[key] = self._transcripts.get(key, "") + event["delta"]
            await self._send_transcript(event["role"], self._transcripts[key], False)
        elif event_type == "bidi_transcript_block":
            self._transcripts.pop((event["role"], event["content_id"]), None)
            await self._send_transcript(event["role"], event["transcript"], True)
            logger.info(
                "Voice transcript completed",
                connection_id=self.context.connection_id,
                role=event["role"],
                character_count=len(event["transcript"]),
            )
        elif event_type == "bidi_audio_start":
            await self._notify_response()
        elif event_type == "bidi_audio_delta":
            await self._send_ws_message(
                {"type": "audioOutput", "audioData": event["audio"]}
            )
        elif event_type == "bidi_response_stop":
            await self._send_ws_message({"type": "contentEnd"})
        elif event_type == "bidi_barge_in":
            # Existing contentStart clears playback; no new browser event needed.
            await self._send_ws_message({"type": "contentStart", "role": "ASSISTANT"})
            await self._send_ws_message({"type": "contentEnd"})
            logger.info(
                "Voice response interrupted", connection_id=self.context.connection_id
            )
        elif event_type == "bidi_tool_use_blocks":
            for tool in event["tool_uses"]:
                await self._send_ws_message(
                    {"type": "toolUse", "toolName": tool["name"]}
                )
        elif event_type == "bidi_connection_restart":
            logger.info(
                "Strands model connection restarting",
                connection_id=self.context.connection_id,
                reason=event["reason"],
                turn_interrupted=event["turn_interrupted"],
            )
            if event["turn_interrupted"]:
                await self._send_ws_message(
                    {"type": "contentStart", "role": "ASSISTANT"}
                )
                await self._send_ws_message({"type": "contentEnd"})
        elif event_type == "bidi_usage":
            logger.info(
                "Voice token usage",
                connection_id=self.context.connection_id,
                # GA's mapping keys follow the Bedrock usage envelope; the
                # typed event properties use snake_case instead.
                input_tokens=event["inputTokens"],
                output_tokens=event["outputTokens"],
            )

    async def _send_transcript(self, role: str, text: str, is_final: bool) -> None:
        if role in {"user", "assistant"}:
            await self._send_ws_message(
                {
                    "type": "userTranscript" if role == "user" else "agentTranscript",
                    "text": text,
                    "isFinal": is_final,
                }
            )

    async def _send_ws_message(self, message: dict[str, Any]) -> None:
        try:
            await self.context.ws.send_json(message)
        except Exception as error:
            logger.warning(
                "Failed to send WebSocket message",
                connection_id=self.context.connection_id,
                message_type=message.get("type"),
                error_type=type(error).__name__,
            )
