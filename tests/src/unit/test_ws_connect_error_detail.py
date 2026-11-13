"""Surfacing *why* a WebSocket connection failed.

Regression coverage for the opaque-error problem behind issue #1464: a failed
``connect()`` used to bubble up only as "Failed to connect to Home Assistant
WebSocket" with the real reason confined to a log line. These tests pin the
reason being captured on the client and surfaced at every call site (the
pooled ``WebSocketManager.get_client`` path, the ``get_connected_ws_client``
helper and the backup capture built on it).

The failures are real: a loopback port nothing listens on refuses the
connection, Core's own ``websocket_api`` rejects a token it never issued, and
a second reader on a new socket takes Core's ``auth_required`` from the client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ha_mcp.backup_zones import fetch_zone
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools.helpers import get_connected_ws_client

from ._core_client import refused_url
from ._ws_transport import spawned_by

REJECTED_TOKEN = "super-secret-token-DO-NOT-LEAK"

# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels (as in test_backup_auth_refusal.py).
_REFUSED_SOCKET_TIMER = pytest.mark.parametrize("expected_lingering_timers", [True])


@pytest.fixture
def refused(socket_enabled: None) -> str:
    """A loopback base URL whose connection is refused."""
    return refused_url()


@pytest.fixture
async def manager() -> AsyncIterator[None]:
    """Leave no pooled connection behind for the next test."""
    yield
    await websocket_manager.disconnect()


async def test_a_refused_connection_is_captured_verbatim(refused: str) -> None:
    """A transport-level failure is captured with its type and message."""
    client = HomeAssistantWebSocketClient(refused, "t", verify_ssl=False)

    assert await client.connect() is False

    assert client.last_connect_error is not None
    assert client.last_connect_error.startswith("ConnectionRefusedError: ")


@_REFUSED_SOCKET_TIMER
async def test_a_rejected_token_is_captured_without_leaking_it(
    core_client: HomeAssistantClient,
) -> None:
    """Core's refusal during the handshake reaches the caller, not the token.

    The reason is built only from the exception text; the token travels in
    the Authorization header / auth payload and is never echoed into a connect
    exception. This pins that invariant against a future change that
    captures more (repr(e), e.args, the URL, or request headers).
    """
    client = HomeAssistantWebSocketClient(
        core_client.base_url, REJECTED_TOKEN, verify_ssl=False
    )

    assert await client.connect() is False

    assert client.last_connect_error is not None
    assert "Invalid token" in client.last_connect_error
    assert REJECTED_TOKEN not in client.last_connect_error


async def test_a_handshake_without_auth_required_is_captured(
    core_client: HomeAssistantClient,
) -> None:
    """A handshake that never asks for auth is reported as such.

    Core always sends ``auth_required``; a second reader, started as the
    client's own reader is created and before it reads, takes the socket's
    first message, so the client never sees it. The client waits out its
    auth timeout (5 s), then must name the missing message as the reason.
    """
    client = HomeAssistantWebSocketClient(
        core_client.base_url, core_client.token, verify_ssl=False
    )
    readers: list[asyncio.Task[Any]] = []

    def take_the_first_message() -> None:
        assert client.websocket is not None
        readers.append(asyncio.create_task(client.websocket.recv(), eager_start=True))

    with spawned_by(client, on_spawn=take_the_first_message):
        assert await client.connect() is False
    await asyncio.gather(*readers, return_exceptions=True)

    assert client.last_connect_error is not None
    assert "Did not receive auth_required" in client.last_connect_error


@_REFUSED_SOCKET_TIMER
async def test_a_later_successful_attempt_clears_the_reason(
    core_client: HomeAssistantClient, hass_access_token: str
) -> None:
    """A new connect() attempt drops the previous reason before running."""
    client = HomeAssistantWebSocketClient(
        core_client.base_url, REJECTED_TOKEN, verify_ssl=False
    )
    assert await client.connect() is False
    assert client.last_connect_error is not None

    client.token = hass_access_token
    try:
        assert await client.connect() is True
        assert client.last_connect_error is None
    finally:
        await client.disconnect()


@pytest.mark.usefixtures("manager")
async def test_pooled_client_names_the_reason(refused: str) -> None:
    """``WebSocketManager.get_client`` appends the reason to its error."""
    with pytest.raises(HomeAssistantConnectionError) as excinfo:
        await websocket_manager.get_client(url=refused, token="t", verify_ssl=False)

    message = str(excinfo.value)
    assert message.startswith("Failed to connect to Home Assistant WebSocket: ")
    assert "ConnectionRefusedError" in message


async def test_connect_helper_puts_the_reason_in_the_details(refused: str) -> None:
    """``get_connected_ws_client`` reports the refusal with its reason."""
    ws_client, error = await get_connected_ws_client(refused, "t", verify_ssl=False)

    assert ws_client is None
    assert error is not None
    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert "ConnectionRefusedError" in json.dumps(error)


@_REFUSED_SOCKET_TIMER
async def test_connect_helper_reports_a_rejected_token_as_auth(
    core_client: HomeAssistantClient,
) -> None:
    """A connect that failed on a rejected token is an auth error, not a
    connection error, and still carries the reason."""
    ws_client, error = await get_connected_ws_client(
        core_client.base_url, REJECTED_TOKEN, verify_ssl=False
    )

    assert ws_client is None
    assert error is not None
    assert error["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert "Invalid token" in json.dumps(error)


@_REFUSED_SOCKET_TIMER
@pytest.mark.parametrize("auth", [True, False], ids=["rejected_token", "refused"])
async def test_backup_capture_raises_a_failed_connect_by_its_code(
    auth: bool, core_client: HomeAssistantClient
) -> None:
    """The backup capture keeps a rejected token an auth error.

    A capture reads Core over its own one-shot WebSocket; a refusal must stay
    a connection error, which the best-effort capture tolerates, while a
    rejected token must not be mistaken for one.
    """
    if auth:
        client = HomeAssistantClient(
            base_url=core_client.base_url, token=REJECTED_TOKEN, verify_ssl=False
        )
    else:
        client = HomeAssistantClient(
            base_url=refused_url(), token="t", verify_ssl=False
        )
    expected = HomeAssistantAuthError if auth else HomeAssistantConnectionError
    try:
        with pytest.raises(expected):
            await fetch_zone(client, "zone.office")
    finally:
        await client.close()
