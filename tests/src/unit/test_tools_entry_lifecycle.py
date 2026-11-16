"""The tools entry's lifecycle and the entry-type dispatch, on a real ``hass`` (#2292).

Core sets entries up and unloads them through its config-entry machinery, so
what is checked is what a user or the server can see:

1. Every service the tools setup registers is gone after unload, so no
   privileged service keeps answering once its entry is removed.
2. Unload drops the cached extra directories and YAML write keys (seen through
   the tools entry's options form) and a reload picks up a rotated caller token.
3. ``entry_type == "server"`` routes to the embedded server entry; anything
   else, including a missing ``entry_type`` (the pre-2.1.0 shape), routes to the
   tools setup, in both directions.
4. The shared ``ha_mcp_tools/*`` WebSocket commands are registered by the tools
   setup and survive its unload.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import WebSocketGenerator

import custom_components.ha_mcp_tools as comp
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_TOOLS,
    TOOLS_ENTRY_TITLE,
)
from custom_components.ha_mcp_tools.websocket_api import constants as c

from ._component_core import ws_call
from ._server_entry import add_server_entry, replace_server, set_up

_TOKEN_KEY = f"{DOMAIN}_auth"
_ALLOWED_PATHS_KEY = f"{DOMAIN}_allowed_paths"
_EXTRA_YAML_KEYS_KEY = f"{DOMAIN}_extra_yaml_keys"


def _add_tools_entry(
    hass: HomeAssistant, data: dict[str, Any] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={} if data is None else data,
    )
    entry.add_to_hass(hass)
    return entry


def _seed_store(hass_storage: dict[str, Any], key: str, data: dict[str, Any]) -> None:
    hass_storage[key] = {"version": 1, "key": key, "data": data}


def _registered_services(hass: HomeAssistant) -> set[str]:
    return set(hass.services.async_services_internal().get(DOMAIN, {}))


async def _fetch_caller_token(hass: HomeAssistant) -> str:
    reply = await hass.services.async_call(
        DOMAIN,
        comp.SERVICE_GET_CALLER_TOKEN,
        {},
        blocking=True,
        return_response=True,
    )
    assert reply is not None
    token = reply["token"]
    assert isinstance(token, str)
    return token


async def _options_form_defaults(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    """What the tools entry's options form shows as the current values."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    assert schema is not None
    defaults: dict[str, Any] = schema({})
    hass.config_entries.options.async_abort(result["flow_id"])
    return defaults


async def test_unload_removes_every_service_setup_registers(
    hass: HomeAssistant,
) -> None:
    """A service added to setup alone would keep answering after the entry is gone."""
    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert _registered_services(hass), "the tools setup registered no services"

    assert await hass.config_entries.async_unload(entry.entry_id) is True

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _registered_services(hass) == set()


async def test_extra_yaml_keys_services_are_registered_by_setup(
    hass: HomeAssistant,
) -> None:
    """The #2292 pair: an empty registered set would also pass the parity test."""
    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True

    assert {
        comp.SERVICE_GET_EXTRA_YAML_KEYS,
        comp.SERVICE_SET_EXTRA_YAML_KEYS,
    } <= _registered_services(hass)


async def test_reload_after_unload_serves_the_rotated_caller_token(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A stale cached token would keep authorising the old one after a reload."""
    _seed_store(hass_storage, _TOKEN_KEY, {"token": "tok-abc"})
    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert await _fetch_caller_token(hass) == "tok-abc"

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    _seed_store(hass_storage, _TOKEN_KEY, {"token": "tok-new"})
    assert await hass.config_entries.async_setup(entry.entry_id) is True

    assert await _fetch_caller_token(hass) == "tok-new"


async def test_unload_reverts_the_extra_directories(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A leftover directory list would keep granting file access after unload."""
    _seed_store(hass_storage, _ALLOWED_PATHS_KEY, {"paths": ["my_scripts"]})
    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert (await _options_form_defaults(hass, entry))["allowed_dirs"] == ["my_scripts"]

    assert await hass.config_entries.async_unload(entry.entry_id) is True

    assert (await _options_form_defaults(hass, entry))["allowed_dirs"] == []


async def test_unload_reverts_the_extra_yaml_write_keys(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The #2292 fix: a leftover list would keep widening the YAML write
    allowlist after the entry that configured it is gone."""
    _seed_store(hass_storage, _EXTRA_YAML_KEYS_KEY, {"keys": ["rest_command"]})
    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert (await _options_form_defaults(hass, entry))["extra_yaml_keys"] == [
        "rest_command"
    ]

    assert await hass.config_entries.async_unload(entry.entry_id) is True

    assert (await _options_form_defaults(hass, entry))["extra_yaml_keys"] == []


@pytest.mark.parametrize(
    "data",
    [{}, {CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS}, {CONF_ENTRY_TYPE: "future_entry_type"}],
    ids=["missing", "explicit_tools", "other_nonserver"],
)
async def test_non_server_entry_type_sets_up_the_tools_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    """Pre-2.1.0 entries carry no ``entry_type`` and must keep working."""
    server = replace_server(monkeypatch)
    entry = _add_tools_entry(hass, data)

    assert await hass.config_entries.async_setup(entry.entry_id) is True

    assert hass.services.has_service(DOMAIN, comp.SERVICE_GET_CALLER_TOKEN)
    server.bring_up.assert_not_awaited()


async def test_server_entry_type_sets_up_the_server_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = replace_server(monkeypatch)
    entry = add_server_entry(hass)

    assert await set_up(hass, entry) is True

    server.bring_up.assert_awaited_once()
    assert _registered_services(hass) == set()


@pytest.mark.parametrize(
    "data",
    [{}, {CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS}, {CONF_ENTRY_TYPE: "future_entry_type"}],
    ids=["missing", "explicit_tools", "other_nonserver"],
)
async def test_non_server_entry_type_unloads_the_tools_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    server = replace_server(monkeypatch)
    tools_entry = _add_tools_entry(hass, data)
    server_entry = add_server_entry(hass)
    assert await set_up(hass, tools_entry) is True
    assert server_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(tools_entry.entry_id) is True

    assert _registered_services(hass) == set()
    server.teardown.assert_not_awaited()


async def test_server_entry_type_unloads_the_server_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = replace_server(monkeypatch)
    tools_entry = _add_tools_entry(hass)
    server_entry = add_server_entry(hass)
    assert await set_up(hass, tools_entry) is True
    assert server_entry.state is ConfigEntryState.LOADED
    services = _registered_services(hass)
    assert services

    assert await hass.config_entries.async_unload(server_entry.entry_id) is True

    server.teardown.assert_awaited_once()
    assert _registered_services(hass) == services


async def test_tools_setup_registers_the_ws_commands_and_unload_keeps_them(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The surface is shared with the server entry and Core has no way to
    unregister a command, so unloading the tools entry must leave it answering."""
    client = await hass_ws_client(hass)
    before = await ws_call(client, c.WS_INFO)
    assert before["success"] is False

    entry = _add_tools_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert (await ws_call(client, c.WS_INFO))["success"] is True

    assert await hass.config_entries.async_unload(entry.entry_id) is True

    assert (await ws_call(client, c.WS_INFO))["success"] is True
