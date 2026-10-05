"""Nova Sonic bidirectional stream session manager for the StayOS Voice Agent.

Manages the lifecycle of a single Nova Sonic bidirectional stream, bridging
audio between a browser WebSocket connection and the Amazon Nova Sonic model.
Each NovaSonicSession instance corresponds to one active voice conversation
with a General Manager.

Responsibilities:
- Open a bidirectional stream to Nova Sonic via InvokeModelWithBidirectionalStream
- Send sessionStart, promptStart, audioInput, toolResult, and sessionEnd events
- Process output events (audioOutput, textOutput, toolUse, contentStart/End)
- Relay audio and transcript events to the browser via the WebSocket connection
- Execute tool handlers with the session's property_id (property-scoped access)
- Track idle timeout via last_activity timestamp

Role in project: Created by server.py per accepted WebSocket connection on
AgentCore Runtime. Each session runs in an isolated microVM, so there is
exactly one NovaSonicSession per container instance. Uses system_prompt.py,
tools_config.py, and tool_handlers.py for session configuration and tool
execution. Communicates with the browser via Starlette WebSocket messages
(provided by the BedrockAgentCoreApp framework).

Environment variables:
    AWS_DEFAULT_REGION: AWS region for the Bedrock Runtime endpoint
"""

import dataclasses
import json
import time
import traceback
import uuid
from typing import Any, Dict, Optional, Set

from aws_lambda_powertools import Logger
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

import os

from system_prompt import SYSTEM_PROMPT
from tool_handlers import dispatch_tool
from tools_config import TOOL_CONFIGURATION

# Module-level logger for structured logging (works outside Lambda via Powertools)
logger: Logger = Logger(service="stayos-voice-agent")

# AWS region from environment (injected by AgentCore Runtime)
AWS_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Nova Sonic model identifier
MODEL_ID: str = "amazon.nova-2-sonic-v1:0"

# Supported languages for voice sessions (Nova Sonic multi-language)
SUPPORTED_LANGUAGES: Set[str] = {"en-US", "es-US", "ja-JP", "zh-CN"}
DEFAULT_LANGUAGE: str = "en-US"

# Inference configuration for Nova Sonic
MAX_TOKENS: int = 1024
TOP_P: float = 0.9
TEMPERATURE: float = 0.7

# Voice ID for audio output (tiffany is the default Nova Sonic female voice)
VOICE_ID: str = "tiffany"

# Idle timeout threshold in seconds (session closes after 60s of no audio input)
IDLE_TIMEOUT_SECONDS: int = 60

# Module-level Bedrock client (connection reuse across all sessions in the process)
# On AgentCore Runtime, credentials are provided via the native boto3 credential chain
# (IAM role attached to the runtime). The EnvironmentCredentialsResolver picks up the
# credentials injected by the AgentCore platform into the container environment.
_bedrock_config: Config = Config(
    endpoint_uri=f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com",
    region=AWS_REGION,
    aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
)
_bedrock_client: BedrockRuntimeClient = BedrockRuntimeClient(config=_bedrock_config)


@dataclasses.dataclass
class SessionContext:
    """State held in memory for one voice session (one WebSocket connection).

    On AgentCore, each session runs in an isolated microVM, so there is
    exactly one SessionContext per container instance.

    Attributes:
        connection_id: Unique ID for this WebSocket connection.
        property_id: From Cognito GetUser custom:propertyId (scopes all DynamoDB queries).
        gm_alias: From Cognito GetUser custom:gmAlias (for logging and personalization).
        language: Language code for Nova Sonic (e.g., "en-US").
        last_activity: Timestamp of last audio input (for idle timeout).
        is_stream_active: Whether the Nova Sonic bidirectional stream is open.
        ws: Reference to the Starlette WebSocket for sending messages.
    """

    connection_id: str
    property_id: str
    gm_alias: str
    language: str
    last_activity: float
    is_stream_active: bool
    ws: Any  # starlette.websockets.WebSocket (provided by BedrockAgentCoreApp)


class NovaSonicSession:
    """Manages a single Nova Sonic bidirectional stream lifecycle.

    One instance per active WebSocket connection. Holds the bidirectional stream
    open for the session duration and provides async methods for sending and
    receiving events. Uses asyncio for concurrent I/O (no threads).

    The session lifecycle:
    1. start() - opens the stream, sends sessionStart + promptStart
    2. send_audio() - called per audio chunk from the browser
    3. handle_output_events() - long-running coroutine processing responses
    4. execute_tool() - called when Nova Sonic requests a tool invocation
    5. close() - sends sessionEnd and tears down the stream
    """

    def __init__(
        self,
        property_id: str,
        gm_alias: str,
        language: str,
        ws: Any,
    ) -> None:
        """Initialize a new Nova Sonic voice session.

        Args:
            property_id: Property scope from identity resolution (partition key for all queries).
            gm_alias: GM identifier from identity resolution (for logging).
            language: Language preference from GM settings (e.g., "en-US").
            ws: The Starlette WebSocket connection to the browser (from BedrockAgentCoreApp).
        """
        # Resolve language - fall back to English if not supported
        resolved_language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

        self.context: SessionContext = SessionContext(
            connection_id=str(uuid.uuid4()),
            property_id=property_id,
            gm_alias=gm_alias,
            language=resolved_language,
            last_activity=time.time(),
            is_stream_active=False,
            ws=ws,
        )

        # Stream response handle (set during start())
        self._stream_response: Optional[Any] = None

        # Prompt and content name identifiers for the Nova Sonic event protocol
        self._prompt_name: str = f"prompt-{self.context.connection_id}"
        self._audio_content_name: str = f"audio-input-{self.context.connection_id}"
        self._audio_content_started: bool = False

        # Counter for unique tool result content names
        self._tool_result_counter: int = 0

        logger.info(
            "NovaSonicSession created",
            connection_id=self.context.connection_id,
            property_id=property_id,
            gm_alias=gm_alias,
            language=resolved_language,
        )

    async def start(self) -> None:
        """Open the bidirectional stream and send sessionStart + promptStart events.

        Opens the Nova Sonic bidirectional stream, then sends the initial
        configuration events following the Nova 2 Sonic protocol:
        1. sessionStart (inference config + turn detection)
        2. promptStart (audio output config, tool config)
        3. System prompt as contentStart/textInput/contentEnd
        4. Audio content start (opens the user audio block)

        Raises:
            Exception: If the Bedrock stream cannot be opened.
        """
        logger.info(
            "Starting Nova Sonic bidirectional stream",
            connection_id=self.context.connection_id,
            model_id=MODEL_ID,
        )

        # Open the bidirectional stream with Nova Sonic (timeout after 10s)
        try:
            import asyncio
            self._stream_response = await asyncio.wait_for(
                _bedrock_client.invoke_model_with_bidirectional_stream(
                    InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Timed out opening Nova Sonic stream (10s)",
                connection_id=self.context.connection_id,
                model_id=MODEL_ID,
            )
            raise RuntimeError("Nova Sonic stream open timed out after 10 seconds")
        except Exception as error:
            logger.error(
                "Failed to open Nova Sonic stream",
                connection_id=self.context.connection_id,
                model_id=MODEL_ID,
                error=str(error),
                error_type=type(error).__name__,
            )
            raise

        logger.info(
            "Nova Sonic stream opened successfully",
            connection_id=self.context.connection_id,
        )
        # nosemgrep: is-function-without-parentheses -- bool field assignment, not a method.
        self.context.is_stream_active = True

        # 1. Send sessionStart event - inference params + turn detection
        # turnDetectionConfiguration is required for Nova 2 Sonic
        session_start_event = {
            "event": {
                "sessionStart": {
                    "inferenceConfiguration": {
                        "maxTokens": MAX_TOKENS,
                        "topP": TOP_P,
                        "temperature": TEMPERATURE,
                    },
                    "turnDetectionConfiguration": {
                        "endpointingSensitivity": "MEDIUM",
                    },
                }
            }
        }
        await self._send_event(session_start_event)

        # 2. Send promptStart event - audio output config and tool config
        prompt_start_event = {
            "event": {
                "promptStart": {
                    "promptName": self._prompt_name,
                    "textOutputConfiguration": {
                        "mediaType": "text/plain",
                    },
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": VOICE_ID,
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                    "toolUseOutputConfiguration": {
                        "mediaType": "application/json",
                    },
                    "toolConfiguration": {
                        "tools": TOOL_CONFIGURATION,
                        "toolChoice": {
                            "auto": {},
                        },
                    },
                }
            }
        }
        await self._send_event(prompt_start_event)

        # 3. Send system prompt as a separate content block (Nova 2 Sonic spec)
        system_content_name = f"system-{self.context.connection_id}"
        await self._send_event({
            "event": {
                "contentStart": {
                    "promptName": self._prompt_name,
                    "contentName": system_content_name,
                    "type": "TEXT",
                    "interactive": False,
                    "role": "SYSTEM",
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                }
            }
        })
        await self._send_event({
            "event": {
                "textInput": {
                    "promptName": self._prompt_name,
                    "contentName": system_content_name,
                    "content": SYSTEM_PROMPT,
                }
            }
        })
        await self._send_event({
            "event": {
                "contentEnd": {
                    "promptName": self._prompt_name,
                    "contentName": system_content_name,
                }
            }
        })

        # 4. Start the user audio content block so audio chunks can be sent
        await self._start_audio_content()

        logger.info(
            "Nova Sonic stream started successfully",
            connection_id=self.context.connection_id,
        )

    async def send_audio(self, audio_base64: str) -> None:
        """Forward a base64-encoded PCM audio chunk to Nova Sonic.

        Sends the audio data as an audioInput event on the bidirectional stream.
        Also resets the idle timer since audio indicates active user engagement.

        Args:
            audio_base64: Base64-encoded 16kHz/16-bit/mono PCM audio chunk.
        """
        # nosemgrep: is-function-without-parentheses -- is_stream_active is a
        # bool dataclass field (see SessionContext), not a method; this is a
        # truthiness check, not a missing call.
        if not self.context.is_stream_active:
            logger.warning(
                "Attempted to send audio on inactive stream",
                connection_id=self.context.connection_id,
            )
            return

        # Ensure the audio content block is open
        if not self._audio_content_started:
            await self._start_audio_content()

        # Send the audio chunk as an audioInput event
        audio_event = {
            "event": {
                "audioInput": {
                    "promptName": self._prompt_name,
                    "contentName": self._audio_content_name,
                    "content": audio_base64,
                }
            }
        }
        await self._send_event(audio_event)

        # Reset idle timer on every audio input
        self.reset_idle_timer()

    async def handle_output_events(self) -> None:
        """Process Nova Sonic output events and route them to the WebSocket.

        Runs as a long-lived coroutine for the session duration. Continuously
        reads events from the Nova Sonic output stream and dispatches them:
        - audioOutput: forward base64 audio to browser
        - textOutput: forward transcript to browser (user or assistant)
        - toolUse: execute tool, send result back to stream, notify browser
        - contentStart/End: forward to browser for UI state management
        - completionEnd: log session completion

        This coroutine exits when the stream closes or an error occurs.
        """
        logger.info(
            "Starting output event handler",
            connection_id=self.context.connection_id,
        )

        try:
            output = await self._stream_response.await_output()
            # nosemgrep: is-function-without-parentheses -- bool field, not a method.
            while self.context.is_stream_active:
                try:
                    result = await output[1].receive()
                    if result is None:
                        # Stream closed by Nova Sonic
                        logger.info(
                            "Nova Sonic stream closed",
                            connection_id=self.context.connection_id,
                        )
                        break

                    # Decode the event payload
                    response_data = result.value.bytes_.decode("utf-8")
                    event_data = json.loads(response_data)

                    await self._process_output_event(event_data)

                except StopAsyncIteration:
                    # Stream exhausted
                    logger.info(
                        "Output stream exhausted",
                        connection_id=self.context.connection_id,
                    )
                    break

        except Exception as error:
            logger.error(
                "Error in output event handler",
                connection_id=self.context.connection_id,
                error=str(error),
                error_repr=repr(error),
                error_type=type(error).__name__,
                traceback=traceback.format_exc(),
            )
            # Notify browser of the stream error
            await self._send_ws_message({
                "type": "error",
                "code": "STREAM_DISCONNECTED",
                "message": "The voice session was interrupted. Please try again.",
            })
        finally:
            # nosemgrep: is-function-without-parentheses -- bool field assignment, not a method.
            self.context.is_stream_active = False

    async def execute_tool(
        self,
        tool_name: str,
        tool_params: Dict[str, Any],
        tool_use_id: str,
    ) -> Dict[str, Any]:
        """Dispatch tool invocation to the appropriate handler.

        Always injects property_id from session context, never from model params.
        Sends the tool result back to the Nova Sonic stream as a toolResult event.

        Args:
            tool_name: The tool name from Nova Sonic's toolUse event.
            tool_params: Parameters extracted from the toolUse event.
            tool_use_id: The toolUseId from Nova Sonic for correlation.

        Returns:
            The tool result dict (success data or unavailability message).
        """
        logger.info(
            "Executing tool",
            connection_id=self.context.connection_id,
            tool_name=tool_name,
            property_id=self.context.property_id,
        )

        # Dispatch to the tool handler (property_id from session, not model)
        result = await dispatch_tool(
            tool_name=tool_name,
            property_id=self.context.property_id,
            params=tool_params,
        )

        # Send the tool result back to Nova Sonic
        self._tool_result_counter += 1
        tool_content_name = f"tool-result-{self._tool_result_counter}"

        # Send contentStart for the tool result
        tool_content_start = {
            "event": {
                "contentStart": {
                    "promptName": self._prompt_name,
                    "contentName": tool_content_name,
                    "interactive": False,
                    "type": "TOOL",
                    "role": "TOOL",
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {
                            "mediaType": "text/plain",
                        },
                    },
                }
            }
        }
        await self._send_event(tool_content_start)

        # Send the tool result content
        tool_result_event = {
            "event": {
                "toolResult": {
                    "promptName": self._prompt_name,
                    "contentName": tool_content_name,
                    "content": json.dumps(result),
                }
            }
        }
        await self._send_event(tool_result_event)

        # Send contentEnd for the tool result
        tool_content_end = {
            "event": {
                "contentEnd": {
                    "promptName": self._prompt_name,
                    "contentName": tool_content_name,
                }
            }
        }
        await self._send_event(tool_content_end)

        logger.info(
            "Tool result sent to Nova Sonic",
            connection_id=self.context.connection_id,
            tool_name=tool_name,
            status=result.get("status"),
        )

        return result

    async def close(self) -> None:
        """Send sessionEnd and close the bidirectional stream gracefully.

        Sends contentEnd (if audio content is open), then sessionEnd to Nova
        Sonic. Marks the stream as inactive regardless of whether the send
        succeeds (best-effort cleanup).
        """
        logger.info(
            "Closing Nova Sonic session",
            connection_id=self.context.connection_id,
        )

        try:
            # nosemgrep: is-function-without-parentheses -- bool field, not a method.
            if self.context.is_stream_active and self._stream_response:
                # Close the audio content block if still open
                if self._audio_content_started:
                    await self._end_audio_content()

                # Send sessionEnd event to terminate the stream
                session_end_event = {
                    "event": {
                        "sessionEnd": {}
                    }
                }
                await self._send_event(session_end_event)

                # Close the input stream
                await self._stream_response.input_stream.close()

        except Exception as error:
            logger.warning(
                "Error during session close (best-effort)",
                connection_id=self.context.connection_id,
                error=str(error),
            )
        finally:
            # nosemgrep: is-function-without-parentheses -- bool field assignment, not a method.
            self.context.is_stream_active = False
            self._audio_content_started = False

        logger.info(
            "Nova Sonic session closed",
            connection_id=self.context.connection_id,
        )

    def reset_idle_timer(self) -> None:
        """Reset the idle timeout by updating the last_activity timestamp.

        Called on every audio input to prevent the session from timing out
        during active conversation. The 60-second idle timeout is checked by
        the server.py idle monitor task.
        """
        self.context.last_activity = time.time()

    def is_idle(self) -> bool:
        """Check if the session has exceeded the idle timeout threshold.

        Returns:
            True if the session has been idle for more than IDLE_TIMEOUT_SECONDS.
        """
        elapsed = time.time() - self.context.last_activity
        return elapsed > IDLE_TIMEOUT_SECONDS

    # -----------------------------------------------------------------------
    # Private helper methods
    # -----------------------------------------------------------------------

    async def _send_event(self, event_dict: Dict[str, Any]) -> None:
        """Serialize and send an event to the Nova Sonic input stream.

        Encodes the event dict as JSON, wraps it in the SDK input chunk format,
        and sends it on the bidirectional stream.

        Args:
            event_dict: The event payload to send.
        """
        event_json = json.dumps(event_dict)
        # Log event type for debugging (truncate content fields to avoid huge logs)
        event_keys = list(event_dict.get("event", {}).keys())
        logger.info(
            "Sending event to Nova Sonic",
            connection_id=self.context.connection_id,
            event_type=event_keys[0] if event_keys else "unknown",
            payload_size=len(event_json),
        )
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=event_json.encode("utf-8")
            )
        )
        await self._stream_response.input_stream.send(chunk)

    async def _send_ws_message(self, message: Dict[str, Any]) -> None:
        """Send a JSON message to the browser WebSocket.

        Handles cases where the WebSocket may already be closed (e.g.,
        browser disconnected during processing).

        Args:
            message: The message dict to send as JSON.
        """
        try:
            await self.context.ws.send_json(message)
        except Exception as error:
            logger.warning(
                "Failed to send WebSocket message",
                connection_id=self.context.connection_id,
                message_type=message.get("type"),
                error=str(error),
            )

    async def _start_audio_content(self) -> None:
        """Send a contentStart event to begin the user audio content block.

        Nova Sonic requires a contentStart before audioInput events can be sent.
        This sets up the audio format configuration for the user's speech input.
        """
        content_start_event = {
            "event": {
                "contentStart": {
                    "promptName": self._prompt_name,
                    "contentName": self._audio_content_name,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 16000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            }
        }
        await self._send_event(content_start_event)
        self._audio_content_started = True

    async def _end_audio_content(self) -> None:
        """Send a contentEnd event to close the user audio content block.

        Called during session close or when the user explicitly stops speaking.
        """
        content_end_event = {
            "event": {
                "contentEnd": {
                    "promptName": self._prompt_name,
                    "contentName": self._audio_content_name,
                }
            }
        }
        await self._send_event(content_end_event)
        self._audio_content_started = False

    async def _process_output_event(self, event_data: Dict[str, Any]) -> None:
        """Route a single Nova Sonic output event to the appropriate handler.

        Inspects the event structure and dispatches to the correct processing
        method based on the event type.

        Args:
            event_data: Parsed JSON from the Nova Sonic output stream.
        """
        event = event_data.get("event", {})

        if "audioOutput" in event:
            await self._handle_audio_output(event["audioOutput"])
        elif "textOutput" in event:
            await self._handle_text_output(event["textOutput"])
        elif "toolUse" in event:
            await self._handle_tool_use(event["toolUse"])
        elif "contentStart" in event:
            await self._handle_content_start(event["contentStart"])
        elif "contentEnd" in event:
            await self._handle_content_end(event["contentEnd"])
        elif "completionEnd" in event:
            logger.info(
                "Completion ended",
                connection_id=self.context.connection_id,
            )
        else:
            # Unknown event type - log for debugging but do not crash
            logger.debug(
                "Unhandled output event type",
                connection_id=self.context.connection_id,
                event_keys=list(event.keys()),
            )

    async def _handle_audio_output(self, audio_event: Dict[str, Any]) -> None:
        """Forward audio output to the browser for playback.

        Sends the base64-encoded 24kHz PCM audio chunk to the WebSocket
        so the frontend can decode and play it via the Web Audio API.

        Args:
            audio_event: The audioOutput event payload from Nova Sonic.
        """
        audio_data = audio_event.get("content", "")
        if audio_data:
            await self._send_ws_message({
                "type": "audioOutput",
                "audioData": audio_data,
            })

    async def _handle_text_output(self, text_event: Dict[str, Any]) -> None:
        """Forward transcript text to the browser.

        Routes to either userTranscript or agentTranscript based on the
        role field in the event.

        Args:
            text_event: The textOutput event payload from Nova Sonic.
        """
        role = text_event.get("role", "").upper()
        text = text_event.get("content", "")

        if role == "USER":
            await self._send_ws_message({
                "type": "userTranscript",
                "text": text,
                "isFinal": True,
            })
        elif role == "ASSISTANT":
            await self._send_ws_message({
                "type": "agentTranscript",
                "text": text,
                "isFinal": True,
            })

    async def _handle_tool_use(self, tool_event: Dict[str, Any]) -> None:
        """Process a tool invocation request from Nova Sonic.

        Executes the requested tool with property-scoped access, sends the
        result back to Nova Sonic, and notifies the browser that a tool is
        being used (for UI indicator).

        Args:
            tool_event: The toolUse event payload from Nova Sonic.
        """
        tool_name = tool_event.get("toolName", "")
        tool_use_id = tool_event.get("toolUseId", "")
        tool_params = tool_event.get("content", {})

        # Parse params if they arrive as a JSON string
        if isinstance(tool_params, str):
            try:
                tool_params = json.loads(tool_params)
            except json.JSONDecodeError:
                tool_params = {}

        # Notify the browser that a tool is being invoked (UI state indicator)
        await self._send_ws_message({
            "type": "toolUse",
            "toolName": tool_name,
        })

        # Execute the tool with session property_id (never from model params)
        await self.execute_tool(
            tool_name=tool_name,
            tool_params=tool_params,
            tool_use_id=tool_use_id,
        )

    async def _handle_content_start(
        self,
        content_event: Dict[str, Any],
    ) -> None:
        """Forward content start to the browser for UI state management.

        Notifies the frontend that the assistant is beginning a response,
        allowing the UI to transition to the 'speaking' or 'processing' state.

        Args:
            content_event: The contentStart event payload from Nova Sonic.
        """
        role = content_event.get("role", "")
        if role == "ASSISTANT":
            await self._send_ws_message({
                "type": "contentStart",
                "role": "ASSISTANT",
            })

    async def _handle_content_end(
        self,
        content_event: Dict[str, Any],
    ) -> None:
        """Forward content end to the browser for UI state management.

        Notifies the frontend that the current content block has ended,
        allowing the UI to transition back to the 'listening' state.

        Args:
            content_event: The contentEnd event payload from Nova Sonic.
        """
        await self._send_ws_message({
            "type": "contentEnd",
        })
