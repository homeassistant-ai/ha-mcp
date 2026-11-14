"""``ha_dev_manage_server(update_source)`` on a real ``hass``.

The server entry is a real config entry (``_server_entry``), its options flow
is the component's own, opened and submitted over Core's REST API, and the
component's ``server_entry_update`` command answers over Core's WebSocket API.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_dev import DevTools

from ._server_entry import add_server_entry
from ._ws_transport import until

PIP_SPEC = "ha-mcp==2.0.0"
SERVER_URL = "http://ha:8123"
EMBEDDED_TARGET = "this server (the embedded ha_mcp_tools in-process entry)"


def server_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """A server entry whose stored options are ``options``."""
    return add_server_entry(hass, options=options)


async def update_source(client: HomeAssistantClient, pip_spec: str) -> dict[str, Any]:
    result: dict[str, Any] = await DevTools(client).ha_dev_manage_server(
        action="update_source", pip_spec=pip_spec
    )
    return result


async def applied(entry: MockConfigEntry, pip_spec: str) -> None:
    """Wait for a deferred write to store ``pip_spec`` in the entry's options."""
    await until(lambda: entry.options.get("pip_spec") == pip_spec)
