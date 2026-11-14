"""``ha_search`` routing on a real ``hass``: a spy on each route it can take.

The search runs on the server's own client against Core. ``component_core_client``
adds the component's commands; ``_component_frames.older_component`` makes the
component's ``ha_mcp_tools/info`` leave out capabilities a release before them
lacked.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

SEARCH = "ha_mcp_tools/search"
DASHBOARDS = "ha_mcp_tools/dashboards"
FELL_BACK = "served via legacy path"


class Routes(NamedTuple):
    """Call-through spies: REST states, the legacy WS bridge, the pooled socket."""

    states: AsyncMock
    bridge: AsyncMock
    component: AsyncMock


@contextmanager
def routes(
    client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
) -> Iterator[Routes]:
    with (
        patch.object(client, "get_states", wraps=client.get_states) as states,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as bridge,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        yield Routes(states, bridge, component)


def bridge_types(spy: AsyncMock) -> list[str]:
    """The type of every message sent over the legacy WS bridge."""
    return [str(c.args[0].get("type")) for c in spy.call_args_list]


def fell_back(response: dict[str, Any]) -> bool:
    return any(FELL_BACK in w for w in response.get("warnings", []))
