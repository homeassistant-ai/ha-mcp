"""The ``oauth_callbacks`` commands the settings panel edits the allowlist with.

The none-mode callback allowlist (#2427) is an option on the server entry;
these commands read it and replace it without reloading the entry. The server
entry is a real config entry in a real ``hass`` and the commands run through
Core's WebSocket API (``component_ws``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.const import (
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    MAX_OAUTH_CALLBACK_LENGTH,
    MAX_OAUTH_CALLBACKS,
    OPT_OAUTH_ALLOW_ANY_CALLBACK,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_WEBHOOK_AUTH,
)
from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_OAUTH_CALLBACKS,
    WS_OAUTH_CALLBACKS_UPDATE,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import build_settings_handlers

from ._component_core import ws_call, ws_result
from ._server_entry import add_server_entry

CALLBACK = "https://chatgpt.example/cb"


async def _read(ws: MockHAClientWebSocket) -> Any:
    return await ws_result(ws, WS_OAUTH_CALLBACKS)


async def _update(ws: MockHAClientWebSocket, **fields: Any) -> Any:
    return await ws_result(ws, WS_OAUTH_CALLBACKS_UPDATE, **fields)


async def _refused(ws: MockHAClientWebSocket, **fields: Any) -> str:
    reply = await ws_call(ws, WS_OAUTH_CALLBACKS_UPDATE, **fields)
    assert reply["success"] is False, reply
    message: str = reply["error"]["message"]
    return message


async def test_an_unedited_entry_reports_the_default_list_in_force(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    add_server_entry(hass)

    result = await _read(component_ws)

    assert result["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert (result["customized"], result["allow_any"]) == (False, False)
    assert result["applies"] is True


async def test_the_read_reports_a_turned_off_list(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The panel says the list is not used while Configure's switch is on."""
    add_server_entry(hass, options={OPT_OAUTH_ALLOW_ANY_CALLBACK: True})

    assert (await _read(component_ws))["allow_any"] is True


async def test_a_malformed_stored_value_still_offers_the_reset(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A non-list refuses every callback; the panel's Restore default must show."""
    add_server_entry(
        hass, options={OPT_OAUTH_REDIRECT_ALLOWLIST: "https://x.example/cb"}
    )

    result = await _read(component_ws)

    assert result["allowlist"] == []
    assert result["customized"] is True


async def test_the_list_is_reported_as_inactive_outside_none_mode(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    add_server_entry(hass, options={OPT_WEBHOOK_AUTH: "ha_auth"})

    assert (await _read(component_ws))["applies"] is False


async def test_a_save_keeps_the_other_options(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    entry = add_server_entry(hass, options={"server_port": 9584})

    result = await _update(component_ws, allowlist=[f" {CALLBACK} ", CALLBACK])

    assert result["saved"] is True
    assert dict(entry.options) == {
        "server_port": 9584,
        OPT_OAUTH_REDIRECT_ALLOWLIST: [CALLBACK],
    }


@pytest.mark.parametrize(
    "bad",
    [
        "http://not-loopback/cb",
        "https://example.com/" + "a" * MAX_OAUTH_CALLBACK_LENGTH,
    ],
    ids=["not_loopback", "over_long"],
)
async def test_an_invalid_callback_saves_nothing_and_is_named(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, bad: str
) -> None:
    entry = add_server_entry(hass)

    result = await _update(component_ws, allowlist=[CALLBACK, bad])

    assert result["saved"] is False
    assert result["invalid"] == [bad]
    assert dict(entry.options) == {}


async def test_saving_the_default_unchanged_keeps_following_it(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The panel's Save on an untouched list must not freeze today's default."""
    entry = add_server_entry(hass, options={"server_port": 9584})

    result = await _update(
        component_ws, allowlist=list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    )

    assert (result["saved"], result["customized"]) == (True, False)
    assert dict(entry.options) == {"server_port": 9584}


async def test_the_list_is_counted_after_normalizing_like_configure(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """51 lines with one duplicate are 50 callbacks, which Configure saves too."""
    entry = add_server_entry(hass)
    lines = [f"https://ex{i}.example/cb" for i in range(MAX_OAUTH_CALLBACKS)]

    result = await _update(component_ws, allowlist=[*lines, lines[0]])

    assert result["saved"] is True
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == lines


async def test_more_callbacks_than_the_cap_save_nothing(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    entry = add_server_entry(hass)
    lines = [f"https://ex{i}.example/cb" for i in range(MAX_OAUTH_CALLBACKS + 1)]

    message = await _refused(component_ws, allowlist=lines)

    assert f"at most {MAX_OAUTH_CALLBACKS} callbacks" in message
    assert dict(entry.options) == {}


async def test_reset_restores_the_default_list(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    entry = add_server_entry(hass, options={OPT_OAUTH_REDIRECT_ALLOWLIST: []})

    result = await _update(component_ws, reset=True)

    assert result["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


async def test_an_update_naming_neither_change_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    entry = add_server_entry(hass, options={"server_port": 9584})

    message = await _refused(component_ws)

    assert "needs allowlist or reset" in message
    assert dict(entry.options) == {"server_port": 9584}


async def test_an_install_without_the_server_entry_is_told_so(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    MockConfigEntry(domain="ha_mcp_tools", data={"entry_type": "tools"}).add_to_hass(
        hass
    )

    message = await _refused(component_ws, allowlist=[CALLBACK])

    assert "no ha_mcp_tools in-process server" in message


@pytest.mark.parametrize(
    "frame",
    [
        {"allowlist": [CALLBACK], "reset": True},
        {"allowlist": [CALLBACK, 3]},
    ],
    ids=["both_changes", "not_strings"],
)
async def test_an_out_of_bounds_frame_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, frame: dict[str, Any]
) -> None:
    add_server_entry(hass)

    reply = await ws_call(component_ws, WS_OAUTH_CALLBACKS_UPDATE, **frame)

    assert reply["success"] is False
    assert reply["error"]["code"] == "invalid_format"


@pytest.mark.parametrize(
    ("command", "fields"),
    [(WS_OAUTH_CALLBACKS, {}), (WS_OAUTH_CALLBACKS_UPDATE, {"reset": True})],
)
async def test_only_an_administrator_can_read_or_change_the_list(
    hass: HomeAssistant,
    component_ws_read_only: MockHAClientWebSocket,
    command: str,
    fields: dict[str, Any],
) -> None:
    entry = add_server_entry(hass, options={OPT_OAUTH_REDIRECT_ALLOWLIST: []})

    reply = await ws_call(component_ws_read_only, command, **fields)

    assert reply["error"]["code"] == "unauthorized"
    assert dict(entry.options) == {OPT_OAUTH_REDIRECT_ALLOWLIST: []}


async def test_the_settings_panel_speaks_the_component_contract(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's handlers copy the command names and capability; they read
    and save the list through the real component."""
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    entry = add_server_entry(hass)
    handlers = build_settings_handlers(server=MagicMock(client=component_core_client))
    save = MagicMock()
    save.json = AsyncMock(return_value={"allowlist": [CALLBACK]})

    read = await handlers["get_oauth_callbacks"](MagicMock())
    saved = await handlers["save_oauth_callbacks"](save)

    assert json.loads(read.body)["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert saved.status_code == 200
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]
