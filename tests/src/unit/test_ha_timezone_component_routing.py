"""Routing tests for ``fetch_ha_timezone`` over the ``ha_mcp_tools`` info handshake.

``ha_get_history`` and the logbook-source ``ha_get_logs`` both localize
timestamps via ``add_timezone_metadata``, which previously issued a fresh
``GET /api/config`` REST call on every single invocation purely to read
``time_zone``. When the component's (already cached, process-lifetime)
``ha_mcp_tools/info`` handshake carries a non-empty ``timezone`` field, that
value is used directly and the REST call is skipped entirely. These tests pin
the routing outcomes: a component reporting a usable timezone serves it with
NO REST call; a component that reports none (an old component, which predates
the field) and a component-less install fall back to the unchanged legacy
``client.get_config()`` path.

Each test runs on the server's own client against a real ``hass``; the
component's ``info`` command is the real handler.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.response_helpers import fetch_ha_timezone


async def test_caps_with_timezone_serves_directly_no_rest_call(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A component reporting a non-empty timezone is used with NO REST call."""
    await hass.config.async_set_time_zone("Europe/London")

    with patch.object(
        component_core_client, "get_config", wraps=component_core_client.get_config
    ) as get_config:
        result = await fetch_ha_timezone(component_core_client)

    assert result == ("Europe/London", False)
    get_config.assert_not_called()


async def test_caps_without_timezone_field_falls_back_to_legacy(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A component whose ``info`` carries no timezone (an old component) → legacy fetch."""
    await hass.config.async_set_time_zone("Asia/Tokyo")

    with (
        patch(
            "custom_components.ha_mcp_tools.websocket_api.system._config_time_zone",
            return_value=None,
        ),
        patch.object(
            component_core_client, "get_config", wraps=component_core_client.get_config
        ) as get_config,
    ):
        result = await fetch_ha_timezone(component_core_client)

    # The zone is Core's own /api/config answer.
    assert result == ("Asia/Tokyo", False)
    get_config.assert_awaited_once()


async def test_no_component_falls_back_to_legacy(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A component-less install (``info`` is ``unknown_command``) → legacy fetch."""
    await hass.config.async_set_time_zone("America/Chicago")

    with patch.object(
        core_client, "get_config", wraps=core_client.get_config
    ) as get_config:
        result = await fetch_ha_timezone(core_client)

    assert result == ("America/Chicago", False)
    get_config.assert_awaited_once()
