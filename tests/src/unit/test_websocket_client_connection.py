"""The client's own connection: what it logs of received frames, and a
connect that is cancelled before Core has let it in."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.websockets.protocol import State
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

from ._websocket_client_core import WS_LOGGER, held_auth
from ._ws_transport import connected_ws, until

APPROVAL = "ha_mcp_approval_response"


async def _dispatched_and_logged(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fire ``data`` as an approval response; what the handler got, and the
    frame as the client logged it."""
    caplog.set_level(logging.DEBUG, logger=WS_LOGGER)
    seen: list[dict[str, Any]] = []

    async def handler(event: dict[str, Any]) -> None:
        seen.append(event)

    async with connected_ws(core_client) as client:
        client.add_event_handler(APPROVAL, handler)
        await client.subscribe_events(APPROVAL)
        hass.bus.async_fire(APPROVAL, data)
        await until(lambda: bool(seen))

    # A single mapping argument is the record's ``args`` itself.
    logged = [
        record.args
        for record in caplog.records
        if record.name == WS_LOGGER
        and record.msg == "WebSocket received: %s"
        and isinstance(record.args, dict)
        and record.args.get("event", {}).get("event_type") == APPROVAL
    ]
    return seen[0]["data"], logged


async def test_an_approval_pin_reaches_the_handler_but_not_the_debug_log(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With debug logging on to diagnose something else, the user's approval
    PIN would land in Home Assistant's log in clear text (#2502); the handler
    still needs it, or nothing could verify it."""
    data = {"token": "tok-123", "decision": "approve", "pin": "2468"}

    dispatched, logged = await _dispatched_and_logged(hass, core_client, caplog, data)

    assert dispatched == data
    (frame,) = logged
    assert frame["event"]["data"] == {
        "token": "tok-123",
        "decision": "approve",
        "pin": "<redacted>",
    }
    assert not [
        r for r in caplog.records if r.name == WS_LOGGER and "2468" in r.getMessage()
    ]


async def test_a_pin_is_redacted_at_any_depth_and_in_any_case(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An automation is free to nest the field or spell it ``PIN``; everything
    else stays in the log line, which is there to diagnose."""
    data = {"a": 1, "nested": {"pin": "2468", "keep": ["x", {"PIN": "1234"}]}}

    dispatched, logged = await _dispatched_and_logged(hass, core_client, caplog, data)

    assert dispatched == data
    (frame,) = logged
    assert frame["event"]["data"] == {
        "a": 1,
        "nested": {"pin": "<redacted>", "keep": ["x", {"PIN": "<redacted>"}]},
    }


async def test_a_connect_cancelled_during_login_closes_socket_and_reader(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool takes a client only once ``connect`` returned, so nothing else
    would ever close what an abandoned connect opened."""
    client = HomeAssistantWebSocketClient(
        core_client.base_url, core_client.token, verify_ssl=False
    )
    async with held_auth(monkeypatch) as held:
        connect = asyncio.create_task(client.connect())
        await held.arrived.wait()
        websocket, reader = client.websocket, client.background_task
        assert websocket is not None and reader is not None

        connect.cancel()
        # Core answers the login and goes back to reading, so it answers the
        # client's close.
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await connect

    assert websocket.state is State.CLOSED
    assert reader.done()


async def test_a_cancelled_connect_does_not_close_the_next_connect(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its cleanup acts on its own socket. Run later, a cleanup that read the
    client's state when it ran closed the next attempt's socket and left its
    own open."""
    client = HomeAssistantWebSocketClient(
        core_client.base_url, core_client.token, verify_ssl=False
    )
    async with held_auth(monkeypatch, first_only=True) as held:
        first = asyncio.create_task(client.connect())
        await held.arrived.wait()
        first_socket = client.websocket
        assert first_socket is not None
        first.cancel()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await first

        try:
            assert await client.connect() is True
            second_socket = client.websocket
            assert second_socket is not None

            await asyncio.sleep(0)
            assert first_socket.state is State.CLOSED
            assert second_socket.state is State.OPEN
            assert client.is_connected
        finally:
            await client.disconnect()
