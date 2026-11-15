"""``ha_config_set_helper``'s name check fails open when its read drops.

Before a storage helper is created, the tool reads ``{type}/list`` to find a
helper with the same name. If that read loses its connection, the create goes
ahead (Home Assistant suffixes a duplicate name itself) and the skipped check
is logged and reported as skipped. A token Core refuses is no outage: the tool reports it at once
instead of logging in again for the create. Core and its WebSocket API are
real; only the socket drops or the token is revoked.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers.registry import NAME_CHECK_SKIPPED_WARNING
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._command_faults import dropped_on, pooled_socket
from ._helper_tool_core import fresh_name, set_up
from ._receipt_faults import revoke
from ._tool_errors import error_of


async def test_a_dropped_name_check_still_creates_the_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(hass, "input_boolean")
    name = fresh_name()
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, "input_boolean/list", socket),
        caplog.at_level(logging.WARNING),
    ):
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", name=name
        )

    assert result["success"] is True
    assert result["action"] == "create"
    assert NAME_CHECK_SKIPPED_WARNING in result["warnings"]
    assert any(
        "input_boolean/list failed; skipped the name check" in r.getMessage()
        for r in caplog.records
    )
    listed = await core_client.send_websocket_message({"type": "input_boolean/list"})
    assert name in {item["name"] for item in listed["result"]}


# Revoking the token closes the server socket, leaving its heartbeat timer.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_refused_token_stops_the_create_at_the_name_check(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(hass, "input_boolean")
    revoke(hass, hass_access_token)

    with caplog.at_level(logging.WARNING), pytest.raises(ToolError) as exc:
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", name=fresh_name()
        )

    assert error_of(exc)["code"] == "AUTH_INVALID_TOKEN"
    assert not any("skipped the name check" in r.getMessage() for r in caplog.records)
