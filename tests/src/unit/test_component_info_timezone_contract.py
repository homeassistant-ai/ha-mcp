"""Cross-seam contract test for the ``ha_mcp_tools/info`` ``timezone`` field.

Pipes the REAL component ``ha_mcp_tools/info`` command (answered by a real
``hass`` whose ``config.time_zone`` is set) through the real server-side
``get_component_caps`` negotiation and ``fetch_ha_timezone`` /
``add_timezone_metadata``, over the server's own client, so a vocabulary/shape
drift between the component's additive ``timezone`` field and the server's
``ComponentCaps`` parsing fails here, mirroring
``test_component_readapi_contract.py``'s bridge pattern for the other component
commands (that file is not edited here; this seam has no cap-gated follow-up
command to add to its ``_REAL_FNS`` map).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.response_helpers import add_timezone_metadata


@pytest.mark.asyncio
async def test_real_info_timezone_serves_metadata_with_no_rest_call(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A real component (hass carrying a time_zone) serves add_timezone_metadata
    straight from the info handshake, so the legacy /api/config REST call is not
    needed."""
    await hass.config.async_set_time_zone("America/New_York")

    with patch.object(
        component_core_client, "get_config", wraps=component_core_client.get_config
    ) as rest_config:
        result = await add_timezone_metadata(
            component_core_client, {"last_changed": "2026-06-12T00:06:00+00:00"}
        )
    rest_config.assert_not_called()

    caps = await get_component_caps(component_core_client)
    assert caps is not None
    assert caps.timezone == "America/New_York"
    assert result["metadata"]["home_assistant_timezone"] == "America/New_York"
    # 00:06 UTC on June 12 = 20:06 EDT on June 11 (date rolls back crossing midnight).
    assert result["data"]["last_changed"] == "2026-06-11T20:06:00-04:00"


@pytest.mark.asyncio
async def test_real_info_without_timezone_falls_back_to_legacy(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A real component whose info carries no timezone (``_do_info`` degrades it
    to None when ``hass.config.time_zone`` is not a usable string) leaves the
    legacy REST path untouched: strictly additive, not a behavior change when
    the field is absent."""
    await hass.config.async_set_time_zone("America/New_York")

    with (
        patch(
            "custom_components.ha_mcp_tools.websocket_api.system._config_time_zone",
            return_value=None,
        ),
        patch.object(
            component_core_client, "get_config", wraps=component_core_client.get_config
        ) as rest_config,
    ):
        result = await add_timezone_metadata(
            component_core_client, {"last_changed": "2026-06-12T00:06:00+00:00"}
        )
        caps = await get_component_caps(component_core_client)

    assert caps is not None
    assert caps.timezone is None
    rest_config.assert_awaited_once()
    # The zone came from Core's own /api/config, converted from the REST answer.
    assert result["metadata"]["home_assistant_timezone"] == "America/New_York"
    assert result["data"]["last_changed"] == "2026-06-11T20:06:00-04:00"
