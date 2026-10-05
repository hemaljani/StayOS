"""BedrockAgentCoreApp WebSocket server for the StayOS Voice Agent.

Entry point for the AgentCore Runtime container. Bridges browser WebSocket
connections to Nova Sonic bidirectional streams, resolves user identity from
the first WebSocket message (Cognito Access Token via GetUser API), and
manages session lifecycles including idle timeout and graceful shutdown on
SIGTERM.

Role in project: The main application server running on AgentCore Runtime.
Implements the AgentCore protocol contract: GET /ping for health status and
WebSocket /ws for voice sessions. Creates one NovaSonicSession per accepted
WebSocket connection and tracks active sessions for HealthyBusy status
reporting.

Environment variables:
    SETTINGS_TABLE_NAME: DynamoDB table holding GM language preferences
    AWS_DEFAULT_REGION: AWS region for boto3 resource initialization
"""

import asyncio
import json
import os
import signal
from datetime import datetime
from typing import Any, Dict, Optional, Set

import boto3
import requests
from aws_lambda_powertools import Logger
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from starlette.websockets import WebSocket, WebSocketDisconnect

from identity_resolver import IdentityError, resolve_identity
from nova_sonic_session import NovaSonicSession

# Module-level logger for structured logging (works outside Lambda via Powertools)
logger: Logger = Logger(service="stayos-voice-agent")

# Server configuration constants
IDLE_CHECK_INTERVAL_SECONDS: int = 5
SHUTDOWN_TIMEOUT_SECONDS: int = 10

# Background task for IMDS credential refresh
_credential_refresh_task: Optional[asyncio.Task] = None

# Active sessions registry for graceful shutdown tracking.
# On AgentCore, each microVM hosts exactly one session, but we keep the set
# for consistent shutdown logic and HealthyBusy reporting.
_active_sessions: Set[NovaSonicSession] = set()

# Shutdown flag - when True, new connections are rejected
_shutting_down: bool = False

# DynamoDB resource for reading GM settings (language preference).
# Initialized at module level for connection reuse across sessions.
_boto_config: BotoConfig = BotoConfig(
    retries={"mode": "standard"},
)
_dynamodb_resource: Any = boto3.resource(
    "dynamodb",
    config=_boto_config,
)

# Settings table name from environment (injected by AgentCore task definition)
SETTINGS_TABLE_NAME: str = os.environ.get("SETTINGS_TABLE_NAME", "")

# Initialize the BedrockAgentCoreApp (Starlette/Uvicorn under the hood)
app: BedrockAgentCoreApp = BedrockAgentCoreApp()


# --- IMDS Credential Management ---
# AgentCore microVMs provide IAM role credentials via IMDS (Instance Metadata Service).
# The Smithy SDK's EnvironmentCredentialsResolver reads from env vars, so we fetch
# IMDS credentials and set them as environment variables, then refresh before expiry.
# This pattern is from the official agentcore-samples (01-bedrock-sonic-ws).


def _get_imdsv2_token() -> Optional[str]:
    """Fetch an IMDSv2 session token (6-hour TTL)."""
    try:
        resp = requests.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            timeout=2,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _fetch_imds_credentials() -> Optional[Dict[str, str]]:
    """Fetch IAM role credentials from IMDS (IMDSv2 preferred, fallback to v1)."""
    try:
        token = _get_imdsv2_token()
        headers = {"X-aws-ec2-metadata-token": token} if token else {}

        # Get the IAM role name
        role_resp = requests.get(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            headers=headers,
            timeout=2,
        )
        if role_resp.status_code != 200:
            logger.error(f"IMDS role lookup failed: HTTP {role_resp.status_code}")
            return None

        role_name = role_resp.text.strip()

        # Get credentials for the role
        creds_resp = requests.get(
            f"http://169.254.169.254/latest/meta-data/iam/security-credentials/{role_name}",
            headers=headers,
            timeout=2,
        )
        if creds_resp.status_code != 200:
            logger.error(f"IMDS credential fetch failed: HTTP {creds_resp.status_code}")
            return None

        creds = creds_resp.json()
        return {
            "AccessKeyId": creds["AccessKeyId"],
            "SecretAccessKey": creds["SecretAccessKey"],
            "Token": creds["Token"],
            "Expiration": creds["Expiration"],
        }
    except Exception as e:
        logger.error(f"IMDS credential fetch error: {e}")
        return None


def _set_credentials_env(creds: Dict[str, str]) -> None:
    """Write IMDS credentials to environment variables for EnvironmentCredentialsResolver."""
    os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["Token"]


async def _credential_refresh_loop() -> None:
    """Background task: refresh IMDS credentials before expiry."""
    while True:
        try:
            creds = _fetch_imds_credentials()
            if creds:
                _set_credentials_env(creds)
                logger.info("IMDS credentials refreshed")
                # Calculate next refresh (5 min before expiry, max 1 hour)
                try:
                    expiration = datetime.fromisoformat(
                        creds["Expiration"].replace("Z", "+00:00")
                    )
                    now = datetime.now(expiration.tzinfo)
                    seconds_until_expiry = (expiration - now).total_seconds()
                    refresh_in = min(max(seconds_until_expiry - 300, 60), 3600)
                except Exception:
                    refresh_in = 3600
                await asyncio.sleep(refresh_in)
            else:
                logger.warning("IMDS credential fetch failed, retrying in 60s")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Credential refresh loop error: {e}")
            await asyncio.sleep(60)


def _load_initial_credentials() -> None:
    """Load IMDS credentials at module init (synchronous, before app.run).

    The background refresh task is started lazily on first WebSocket connection
    since asyncio.create_task requires a running event loop.
    """
    # If credentials already set (local dev), skip IMDS
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        logger.info("Credentials already in environment (local mode), skipping IMDS")
        return

    # Fetch initial credentials from IMDS
    creds = _fetch_imds_credentials()
    if creds:
        _set_credentials_env(creds)
        logger.info("Initial IMDS credentials loaded successfully")
    else:
        logger.error("Failed to load initial IMDS credentials - Bedrock calls will fail")


# Load credentials synchronously at module init (before app.run())
_load_initial_credentials()


@app.ping
def ping_handler() -> PingStatus:
    """AgentCore health check endpoint (GET /ping).

    Returns HealthyBusy while any voice session is active, signaling AgentCore
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
    """Handle a single voice agent WebSocket session.

    Implements the AgentCore WebSocket protocol contract on /ws port 8080.
    Each invocation corresponds to one GM voice session in an isolated microVM.

    Session lifecycle:
        1. Accept the WebSocket connection
        2. Receive the first message (identity verification payload)
        3. Extract propertyId and gmAlias via Cognito GetUser
        4. Read GM language preference from DynamoDB settings table
        5. Create NovaSonicSession and start the Nova Sonic stream
        6. Run the message loop (audioInput, sessionEnd routing)
        7. Clean up on disconnect or error

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

    # Step 2: Read GM language preference from the settings table
    language = await _get_gm_language(property_id, gm_alias)

    # Step 3: Create the Nova Sonic session for this connection
    session = NovaSonicSession(
        property_id=property_id,
        gm_alias=gm_alias,
        language=language,
        ws=websocket,
    )
    _active_sessions.add(session)

    # Background tasks for this session
    output_task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None

    try:
        # Start the Nova Sonic bidirectional stream
        await session.start()

        # Notify browser that session is ready
        await websocket.send_json({"type": "sessionStarted"})

        # Start the output event handler as a background task
        output_task = asyncio.create_task(session.handle_output_events())

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
        # Cleanup: close the Nova Sonic stream, cancel background tasks,
        # and remove from active sessions registry
        await _cleanup_session(session, output_task, idle_task)


async def _process_identity_message(websocket: WebSocket) -> Dict[str, str] | None:
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

    Used when identity verification fails or other pre-session errors occur.
    Sends a structured error JSON message before closing the connection.

    Args:
        websocket: The active Starlette WebSocket connection.
        code: Machine-readable error code (e.g., "IDENTITY_FAILED").
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


async def _get_gm_language(property_id: str, gm_alias: str) -> str:
    """Read the GM's language preference from the settings table.

    Queries DynamoDB for the GM's settings record and returns the language
    from audioPreferences.language. Falls back to "en-US" if the record
    doesn't exist or the field is missing.

    Args:
        property_id: The property ID (for logging context).
        gm_alias: The GM's alias (partition key for settings table).

    Returns:
        Language code string (e.g., "en-US", "es-US", "ja-JP", "zh-CN").
    """
    if not SETTINGS_TABLE_NAME:
        logger.warning("SETTINGS_TABLE_NAME not configured, using default language")
        return "en-US"

    try:
        # GetItem on the settings table - gmAlias is the sole partition key
        settings_table = _dynamodb_resource.Table(SETTINGS_TABLE_NAME)
        response = settings_table.get_item(
            Key={"gmAlias": gm_alias}
        )
        # Language is nested under audioPreferences.language
        item = response.get("Item", {})
        audio_prefs = item.get("audioPreferences", {})
        language = audio_prefs.get("language", item.get("language", "en-US"))
        logger.info(
            "GM language preference loaded",
            extra={
                "property_id": property_id,
                "gm_alias": gm_alias,
                "language": language,
            },
        )
        return language

    except ClientError as error:
        logger.error(
            "Failed to read GM settings, defaulting to en-US",
            extra={
                "property_id": property_id,
                "gm_alias": gm_alias,
                "error": str(error),
            },
        )
        return "en-US"


async def _message_loop(session: NovaSonicSession, websocket: WebSocket) -> None:
    """Run the WebSocket message loop, routing messages by type.

    Continuously receives text messages from the WebSocket and dispatches
    them to the appropriate handler based on the 'type' field. Exits when
    the client disconnects (raises WebSocketDisconnect) or sends sessionEnd.

    Supported message types:
    - audioInput: forward audio chunk to Nova Sonic stream
    - sessionEnd: gracefully close the session

    Args:
        session: The NovaSonicSession for this connection.
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

        if msg_type == "audioInput":
            # Forward audio chunk to Nova Sonic
            audio_data = message.get("audioData", "")
            if audio_data:
                await session.send_audio(audio_data)

        elif msg_type == "sessionEnd":
            # Client requested session end - close gracefully
            logger.info(
                "Client requested session end",
                extra={"connection_id": session.context.connection_id},
            )
            await session.close()
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
    session: NovaSonicSession,
    websocket: WebSocket,
) -> None:
    """Monitor session activity and close after 60s of inactivity.

    Checks the session's last_activity timestamp every IDLE_CHECK_INTERVAL_SECONDS.
    If the session has been idle beyond the threshold, sends a sessionEnded event
    to the browser and closes both the Nova Sonic stream and the WebSocket.

    Args:
        session: The NovaSonicSession to monitor.
        websocket: The WebSocket connection to close on timeout.
    """
    try:
        # nosemgrep: is-function-without-parentheses -- is_stream_active is a
        # bool dataclass field (see SessionContext), not a method.
        while session.context.is_stream_active:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)

            if session.is_idle():
                logger.info(
                    "Session idle timeout reached",
                    extra={"connection_id": session.context.connection_id},
                )
                # Notify browser before closing
                try:
                    await websocket.send_json({
                        "type": "sessionEnded",
                        "reason": "idle_timeout",
                    })
                except Exception:
                    pass  # WebSocket may already be closed by client

                await session.close()

                try:
                    await websocket.close()
                except Exception:
                    pass  # Best-effort close

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
    session: NovaSonicSession,
    output_task: asyncio.Task[None] | None,
    idle_task: asyncio.Task[None] | None,
) -> None:
    """Clean up all resources for a disconnected or ended session.

    Closes the Nova Sonic stream (if active), cancels background tasks,
    and removes the session from the active sessions registry.

    Args:
        session: The NovaSonicSession to clean up.
        output_task: The output event handler task (may be None).
        idle_task: The idle monitor task (may be None).
    """
    logger.info(
        "Cleaning up session",
        extra={"connection_id": session.context.connection_id},
    )

    # Close the Nova Sonic stream if still active
    # nosemgrep: is-function-without-parentheses -- bool field, not a method.
    if session.context.is_stream_active:
        await session.close()

    # Cancel background tasks
    if output_task and not output_task.done():
        output_task.cancel()
        try:
            await output_task
        except asyncio.CancelledError:
            pass

    if idle_task and not idle_task.done():
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass

    # Remove from the active sessions registry
    _active_sessions.discard(session)

    logger.info(
        "Session cleanup complete",
        extra={"connection_id": session.context.connection_id},
    )


def _handle_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM signal handler - triggers graceful shutdown via asyncio.

    Schedules the shutdown coroutine on the running event loop. This ensures
    the shutdown logic runs within the asyncio context where it can close
    WebSocket connections and Nova Sonic streams.

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
    """Trigger the application shutdown by closing all active sessions.

    Called from the SIGTERM handler via the event loop. Notifies all connected
    clients, closes their Nova Sonic streams, then raises SystemExit.
    """
    logger.info(
        "Shutting down active sessions from SIGTERM",
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

    logger.info("All sessions closed, server shutting down")

    # Raise SystemExit to stop the Uvicorn runner
    raise SystemExit(0)


async def _shutdown_session(session: NovaSonicSession) -> None:
    """Close a single session during graceful shutdown.

    Sends a sessionEnded event to the browser, closes the Nova Sonic stream,
    and closes the WebSocket connection.

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
        except Exception:
            pass  # WebSocket may already be closed

        # Close the Nova Sonic stream
        await session.close()

        # Close the WebSocket
        try:
            await session.context.ws.close()
        except Exception:
            pass  # Best-effort close

    except Exception as error:
        logger.warning(
            "Error closing session during shutdown",
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

    logger.info(
        "Starting voice agent server on AgentCore Runtime",
        extra={
            "settings_table": SETTINGS_TABLE_NAME,
        },
    )

    # BedrockAgentCoreApp.run() starts Uvicorn on port 8080 with /ping and /ws
    app.run(log_level="info")
