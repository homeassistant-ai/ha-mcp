"""Cross-seam contract test for the ``ha_mcp_tools/info`` ``timezone`` field.

Pipes the REAL component ``_do_info()`` (driven by a real ``FakeHass``/
``FakeConfig`` carrying a ``time_zone``) through the real server-side
``get_component_caps`` negotiation and ``_fetch_ha_timezone`` /
``add_timezone_metadata`` — so a vocabulary/shape drift between the
component's additive ``timezone`` field and the server's ``ComponentCaps``
parsing fails here, mirroring ``test_component_readapi_contract.py``'s bridge
pattern for the other component commands (that file is not edited here; this
seam has no cap-gated follow-up command to add to its ``_REAL_FNS`` map).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import component_api
from ha_mcp.tools.util_helpers import add_timezone_metadata

from .test_component_ws_search import FakeConfig, FakeHass, wsapi


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy ``get_config`` REST fetch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_config_calls = 0

    async def get_config(self) -> dict[str, Any]:
        self.get_config_calls += 1
        return {"time_zone": "UTC"}


def _real_info_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose ``info`` reply is the REAL ``_do_info(hass)`` result."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        return {"success": True, "result": wsapi._do_info(hass)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_real_info_timezone_serves_metadata_with_no_rest_call() -> None:
    """A real component (FakeHass carrying a time_zone) serves add_timezone_metadata
    straight from the info handshake — the legacy /api/config REST call never fires."""
    hass = FakeHass(config=FakeConfig(time_zone="America/New_York"))
    ws = _real_info_ws(hass)
    client = RoutingClient()

    with patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    ):
        result = await add_timezone_metadata(
            client, {"last_changed": "2026-06-12T00:06:00+00:00"}
        )

    assert result["metadata"]["home_assistant_timezone"] == "America/New_York"
    # 00:06 UTC on June 12 = 20:06 EDT on June 11 (date rolls back crossing midnight).
    assert result["data"]["last_changed"] == "2026-06-11T20:06:00-04:00"
    assert client.get_config_calls == 0


@pytest.mark.asyncio
async def test_real_info_without_timezone_falls_back_to_legacy() -> None:
    """A real component whose hass has no ``config`` (timezone degrades to None,
    per ``_do_info``'s own contract) leaves the legacy REST path untouched —
    strictly additive, not a behavior change when the field is absent."""
    hass = FakeHass()  # no config= passed -> _do_info's timezone is None
    ws = _real_info_ws(hass)
    client = RoutingClient()

    with patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    ):
        result = await add_timezone_metadata(
            client, {"last_changed": "2026-06-12T00:06:00+00:00"}
        )

    assert result["metadata"]["home_assistant_timezone"] == "UTC"
    assert client.get_config_calls == 1
