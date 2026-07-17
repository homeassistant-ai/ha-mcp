"""Routing tests for ``_fetch_ha_timezone`` over the ``ha_mcp_tools`` info handshake.

``ha_get_history`` and the logbook-source ``ha_get_logs`` both localize
timestamps via ``add_timezone_metadata``, which previously issued a fresh
``GET /api/config`` REST call on every single invocation purely to read
``time_zone``. When the component's (already cached, process-lifetime)
``ha_mcp_tools/info`` handshake carries a non-empty ``timezone`` field, that
value is used directly and the REST call is skipped entirely. These tests pin
the four routing outcomes the task called out: a component reporting a usable
timezone serves it with NO REST call; an old component (no ``timezone`` key), a
component-less install, and a component reporting an empty string all fall back
to the unchanged legacy ``client.get_config()`` path.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api
from ha_mcp.tools.util_helpers import _fetch_ha_timezone

from ._component_routing_helpers import make_ws, patch_ws

# _fetch_ha_timezone never sends a second component command — it only consults
# the cached info probe — so this placeholder is never actually dispatched.
_UNUSED_COMMAND = "ha_mcp_tools/__unused__"

_CAPS_BASE = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": [],
    "limits": {},
}


class RoutingClient:
    """Credentialed HA client spy: tallies legacy ``get_config`` REST fetches."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_config_calls = 0

    async def get_config(self) -> dict[str, Any]:
        self.get_config_calls += 1
        return {"time_zone": "UTC"}


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_caps_with_timezone_serves_directly_no_rest_call() -> None:
    """A component reporting a non-empty timezone is used with NO REST call."""
    ws = make_ws(
        _UNUSED_COMMAND,
        info_result={**_CAPS_BASE, "timezone": "Europe/London"},
    )
    client = RoutingClient()

    with patch_ws(ws, component_api):
        result = await _fetch_ha_timezone(client)

    assert result == ("Europe/London", False)
    assert client.get_config_calls == 0


@pytest.mark.asyncio
async def test_caps_without_timezone_field_falls_back_to_legacy() -> None:
    """An old component (``info`` predates the ``timezone`` field) → legacy fetch."""
    ws = make_ws(_UNUSED_COMMAND, info_result=dict(_CAPS_BASE))  # no "timezone" key
    client = RoutingClient()

    with patch_ws(ws, component_api):
        result = await _fetch_ha_timezone(client)

    assert result == ("UTC", False)
    assert client.get_config_calls == 1


@pytest.mark.asyncio
async def test_no_component_falls_back_to_legacy() -> None:
    """A component-less install (``info`` is ``unknown_command``) → legacy fetch."""
    ws = make_ws(
        _UNUSED_COMMAND,
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient()

    with patch_ws(ws, component_api):
        result = await _fetch_ha_timezone(client)

    assert result == ("UTC", False)
    assert client.get_config_calls == 1


@pytest.mark.asyncio
async def test_empty_string_timezone_falls_back_to_legacy() -> None:
    """A component reporting an empty (unset) timezone → legacy fetch, not "" ."""
    ws = make_ws(_UNUSED_COMMAND, info_result={**_CAPS_BASE, "timezone": ""})
    client = RoutingClient()

    with patch_ws(ws, component_api):
        result = await _fetch_ha_timezone(client)

    assert result == ("UTC", False)
    assert client.get_config_calls == 1
