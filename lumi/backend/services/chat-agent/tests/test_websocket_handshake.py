"""Exercise the chat handler's browser upgrade without AWS calls or live tokens."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.asyncio
@pytest.mark.parametrize("offered,selected", [
    ("base64UrlBearerAuthorization.ZHVtbXk, base64UrlBearerAuthorization", "base64UrlBearerAuthorization"),
    ("base64UrlBearerAuthorization", "base64UrlBearerAuthorization"),
    ("base64UrlBearerAuthorization.ZHVtbXk", None),
    ("", None),
])
async def test_chat_upgrade_selects_public_sentinel_and_still_checks_identity(
    monkeypatch, offered, selected,
):
    import server

    websocket = SimpleNamespace(
        headers={"sec-websocket-protocol": offered}, accept=AsyncMock(),
    )
    identity = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_shutting_down", False)
    monkeypatch.setattr(server, "_process_identity_message", identity)

    await server.websocket_handler(websocket, None)

    if selected:
        websocket.accept.assert_awaited_once_with(subprotocol=selected)
    else:
        websocket.accept.assert_awaited_once_with()
    identity.assert_awaited_once_with(websocket)
