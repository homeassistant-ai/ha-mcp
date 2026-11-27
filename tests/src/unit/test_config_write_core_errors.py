"""Core 2026.12's config views list each error of a refused automation or
script with its path; the set tools pass that list through as ``errors``.

Core's own config views refuse the config on a real ``hass``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._config_writes import automation_tools, script_tools
from ._tool_errors import error_body

pytestmark = pytest.mark.skipif(
    (MAJOR_VERSION, MINOR_VERSION) < (2026, 12),
    reason="Core's config views list the errors since 2026.12",
)

# Two independent mistakes: the state trigger names no entity, the delay is
# not a time.
_BAD_ACTIONS = [{"delay": "not a time"}]


def _paths(body: dict[str, Any]) -> list[list[str | int]]:
    errors = body["errors"]
    assert all(isinstance(e["message"], str) and e["message"] for e in errors)
    return [e["path"] for e in errors]


@pytest.mark.parametrize("with_component", [False, True], ids=["core", "component"])
async def test_a_refused_automation_lists_every_error_core_found(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    with_component: bool,
) -> None:
    """Without the component Core's config view refuses the write; with it the
    component's validator refuses it first, with the same list."""
    if with_component:
        websocket_api.async_register_commands(hass)
    tools = await automation_tools(hass, core_client, monkeypatch)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            config={
                "alias": "Broken",
                "triggers": [{"trigger": "state"}],
                "actions": _BAD_ACTIONS,
            },
            MandatoryBPS=False,
        )

    body = error_body(exc_info)
    assert body["error"]["code"] == "VALIDATION_FAILED"
    sections = {path[0] for path in _paths(body)}
    assert {"triggers", "actions"} <= sections


async def test_a_refused_script_lists_every_error_core_found(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = await script_tools(hass, core_client, monkeypatch)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(
            script_id="broken",
            config={"alias": "Broken", "sequence": _BAD_ACTIONS},
            MandatoryBPS=False,
        )

    body = error_body(exc_info)
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert [path[0] for path in _paths(body)] == ["sequence"]


async def test_an_unknown_trigger_after_the_first_error_gets_its_closest_matches(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's message names only the first error; a renamed trigger further
    down the list still gets the registered keys nearest to it."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "schedule", {})
    await hass.async_block_till_done()
    tools = await script_tools(hass, core_client, monkeypatch)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(
            script_id="broken",
            config={
                "alias": "Broken",
                "sequence": [
                    *_BAD_ACTIONS,
                    {"wait_for_trigger": [{"trigger": "schedule.turned_on"}]},
                ],
            },
            MandatoryBPS=False,
        )

    body = error_body(exc_info)
    assert "schedule.turned_on" not in body["error"]["message"]
    assert "schedule.block_started" in body["closest_matches"]
    assert any("'schedule.turned_on'" in s for s in body["error"]["suggestions"])
