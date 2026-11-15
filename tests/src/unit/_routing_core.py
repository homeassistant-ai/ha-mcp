"""Real-Core helpers for the component routing tests.

The server's client talks to a real ``hass``; these give a test the pooled
socket both the component reads and the legacy WebSocket bridge use, the
command types sent on it, and a component command Core holds until the
client's wait for it has run out.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    get_websocket_client,
)
from ha_mcp.storage_helper_types import SIMPLE_HELPER_TYPES
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.tools_config_helpers import HelperConfigTools


async def pooled_socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the component reads and the legacy bridge share."""
    return await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )


def sent_types(spy: AsyncMock) -> list[str]:
    """The command types a ``send_command`` spy saw, in order."""
    return [str(call.args[0]) for call in spy.call_args_list]


def sent_frames(spy: AsyncMock, command: str) -> list[dict[str, Any]]:
    """The keyword arguments of every ``command`` a ``send_command`` spy saw."""
    return [dict(call.kwargs) for call in spy.call_args_list if call.args[0] == command]


@asynccontextmanager
async def held_command(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    command: str,
    *,
    when: Callable[[dict[str, Any]], bool] = lambda msg: True,
) -> AsyncIterator[None]:
    """Core holds ``command`` (each message ``when`` accepts) until the client's
    wait for its answer has run out; the handler then runs and answers.

    One real command opens the client's socket under the normal wait; the wait
    is then cut so only the held request runs out of it.
    """
    held = asyncio.Event()
    release = asyncio.Event()
    answered = asyncio.Event()
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    async def hold(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        if when(msg):
            held.set()
            await release.wait()
            core_handler(core, connection, msg)
            answered.set()
        else:
            core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, command, websocket_api.async_response(hold), schema
    )
    await client.send_websocket_message({"type": "ping"})
    try:
        with patch.object(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0.5):
            yield
    finally:
        # Answer the held request while the client's socket is still open.
        release.set()
        if held.is_set():
            async with asyncio.timeout(5):
                await answered.wait()
            # Core writes replies in order: the pong means the held reply is out.
            await client.send_websocket_message({"type": "ping"})
    assert held.is_set(), f"{command} was never sent"


async def setup_storage_booleans(hass: HomeAssistant) -> None:
    """Core's registry API and storage input_booleans."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "input_boolean", {})


async def setup_every_simple_type(hass: HomeAssistant) -> None:
    """Set up every storage helper type, as ``default_config`` does.

    ``helper_type="all"`` reads ``<type>/list`` for each type the component
    does not enumerate, so each must answer.
    """
    for domain in sorted(SIMPLE_HELPER_TYPES):
        assert await async_setup_component(hass, domain, {})


async def list_helpers(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    """``ha_config_list_helpers`` on ``client``."""
    return cast(
        "dict[str, Any]",
        await HelperConfigTools(client).ha_config_list_helpers(**kwargs),
    )


@contextmanager
def invalidations(module: ModuleType) -> Iterator[MagicMock]:
    """A spy on ``module``'s capability invalidation; the real one still runs."""
    with patch.object(
        module, "invalidate_caps", MagicMock(wraps=invalidate_caps)
    ) as spy:
        yield spy
