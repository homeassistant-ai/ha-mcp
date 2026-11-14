"""The ``ha_mcp_tools/*`` command surface on a real ``hass``: handshake, gate, schemas.

``component_ws`` registers the component's commands on Core's WebSocket API and
connects an admin; ``component_ws_read_only`` connects a non-admin. Every reply,
including each refusal, is Core's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import (
    automation_config,
    card_definitions,
    config_references,
    core_contract,
    entity_restore,
    flow_outline,
    flow_record,
    helper_collections,
    todo_items,
)
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import (
    COMPONENT_VERSION,
    DOMAIN,
    TOOLS_ENTRY_TITLE,
)
from custom_components.ha_mcp_tools.websocket_api import constants as c

from ._component_core import ws_call, ws_result

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The commands this file gates, each with the fields its schema requires.
_GATED_COMMANDS: dict[str, dict[str, Any]] = {
    c.WS_INFO: {},
    c.WS_SEARCH: {},
    c.WS_OVERVIEW: {},
    c.WS_HELPERS_LIST: {},
    c.WS_STATES: {"entity_ids": []},
    c.WS_BLUEPRINT_GET: {"domain": "automation", "path": "x.yaml"},
    c.WS_DEVICE_GET: {"device_id": "d1"},
    c.WS_DEVICE_LIST: {},
    c.WS_ENTITY_ENRICH: {"entity_ids": []},
    c.WS_EXPOSURE: {},
    c.WS_CONFIG_ENTRIES: {},
    c.WS_REGISTRY_LOOKUP: {"entity_ids": ["light.x"]},
    c.WS_SYSTEM_SNAPSHOT: {},
    c.WS_ENTITY_LOOKUP: {"unique_id": "x"},
    c.WS_REGISTRIES: {"registries": []},
    c.WS_CALL_EVENT: {"event_type": "probe_event"},
}


def _registered_schema(hass: HomeAssistant, command: str) -> Any:
    """The schema Core validates ``command``'s messages with."""
    return hass.data[core_websocket_api.DOMAIN][command][1]


async def test_info_advertises_the_handshake_contract(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The server negotiates every component command from this one reply."""
    await hass.config.async_set_time_zone("America/New_York")

    info = await ws_result(component_ws, c.WS_INFO)

    assert info["schema_version"] == 1
    assert info["component_version"] == COMPONENT_VERSION
    assert info["capabilities"] == list(c.CAPABILITIES)
    # config_get was withdrawn before release (raw_config freshness lags the
    # config file between write and reload) — it must not be advertised.
    assert "config_get" not in info["capabilities"]
    assert info["limits"] == {"max_results": 500, "max_body_bytes": 1_000_000}
    assert info["timezone"] == "America/New_York"


async def test_info_reports_whether_the_tools_services_are_registered(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Both entry types serve this surface (#2289), so the server's
    filesystem/YAML gate reads ``tools_services`` instead of inferring the
    tools entry from ``info`` answering (#2292)."""
    assert (await ws_result(component_ws, c.WS_INFO))["tools_services"] is False

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    assert (await ws_result(component_ws, c.WS_INFO))["tools_services"] is True


def test_info_reports_the_version_hacs_installed() -> None:
    """A bump that touches manifest.json but not COMPONENT_VERSION (or the
    reverse) makes ``ha_mcp_tools/info`` report a version HACS did not
    install. That both equal the server version is the Component Version
    Gate's job in pr.yml (docs/agents/custom-component.md, Version cycle).
    """
    manifest_path = _REPO_ROOT / "custom_components" / DOMAIN / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == COMPONENT_VERSION


async def test_every_command_is_registered_and_config_get_is_not(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A command dropped from (or added to) the registration fails here."""
    registered = {
        command
        for command in hass.data[core_websocket_api.DOMAIN]
        if command.startswith(f"{DOMAIN}/")
    }

    assert registered == {
        *_GATED_COMMANDS,
        c.WS_DASHBOARDS,
        c.WS_DASHBOARD_EDIT,
        c.WS_SERVICES_LIST,
        c.WS_REFERENCE_DATA,
        c.WS_SERVER_ENTRY,
        c.WS_SERVER_ENTRY_UPDATE,
        c.WS_CALL_SERVICE,
        c.WS_BULK_CALL_SERVICE,
        c.WS_TEMPLATE_DIAGNOSE,
        c.WS_LOGBOOK_EVENTS,
        c.WS_TRACE_CONTEXT,
        c.WS_OAUTH_CALLBACKS,
        c.WS_OAUTH_CALLBACKS_UPDATE,
        *helper_collections.COMMANDS,
        *entity_restore.COMMANDS,
        core_contract.COMMAND,
        core_contract.INTEGRATION_COMMAND,
        "ha_mcp_tools/statistics_units",
        card_definitions.WS_DASHBOARD_CARDS,
        todo_items.WS_TODO_ITEM,
        automation_config.WS_AUTOMATION_CONFIG,
        config_references.WS_CONFIG_REFERENCES,
        flow_record.WS_FLOW_RECORD,
        flow_outline.WS_FLOW_OUTLINE,
    }
    assert "ha_mcp_tools/config_get" not in registered


@pytest.mark.parametrize(("command", "params"), list(_GATED_COMMANDS.items()))
async def test_a_non_admin_is_refused_every_command(
    component_ws_read_only: MockHAClientWebSocket,
    command: str,
    params: dict[str, Any],
) -> None:
    reply = await ws_call(component_ws_read_only, command, **params)

    assert reply["success"] is False
    assert reply["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("command", "params"), list(_GATED_COMMANDS.items()))
async def test_an_admin_gets_a_result_from_every_command(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    command: str,
    params: dict[str, Any],
) -> None:
    # Every install has Core's homeassistant integration, whose exposed-entities
    # store the exposure read uses.
    assert await async_setup_component(hass, "homeassistant", {})
    result = await ws_result(component_ws, command, **params)

    assert isinstance(result, dict)


@pytest.mark.parametrize(
    ("command", "params", "defaults"),
    [
        (
            c.WS_SEARCH,
            {"query": "kitchen"},
            {
                "exact": True,
                "include_hidden": True,
                "include_config": False,
                "limit": c.DEFAULT_LIMIT,
                "offset": 0,
            },
        ),
        (c.WS_OVERVIEW, {}, {"include_notifications": True, "include_repairs": True}),
        (c.WS_HELPERS_LIST, {}, {"include_flow_helpers": True}),
        # Device-only reads stay minimal.
        (c.WS_DEVICE_GET, {"device_id": "d1"}, {"include_entities": False}),
        (
            c.WS_SYSTEM_SNAPSHOT,
            {},
            {
                "include_states": True,
                "include_entities": True,
                "include_issues": True,
                "include_config_entries": True,
            },
        ),
    ],
)
async def test_omitted_options_take_their_documented_defaults(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    command: str,
    params: dict[str, Any],
    defaults: dict[str, Any],
) -> None:
    validated = _registered_schema(hass, command)({"id": 1, "type": command, **params})

    assert {key: validated[key] for key in defaults} == defaults


async def test_search_adds_no_result_fields_the_caller_did_not_ask_for(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    validated = _registered_schema(hass, c.WS_SEARCH)({"id": 1, "type": c.WS_SEARCH})

    assert "result_fields" not in validated


async def test_config_entries_lists_everything_without_a_filter(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    schema = _registered_schema(hass, c.WS_CONFIG_ENTRIES)

    assert schema({"id": 1, "type": c.WS_CONFIG_ENTRIES}) == {
        "id": 1,
        "type": c.WS_CONFIG_ENTRIES,
        "include_subentry_data": False,
    }


@pytest.mark.parametrize(
    ("command", "params"),
    [
        (c.WS_SEARCH, {"result_fields": ["is_group", "member_entity_ids"]}),
        (c.WS_STATES, {"entity_ids": ["light.a", "light.b"]}),
        (c.WS_BLUEPRINT_GET, {"domain": "script", "path": "user/x.yaml"}),
        (c.WS_DEVICE_GET, {"device_id": "d1", "include_entities": True}),
        (c.WS_CONFIG_ENTRIES, {"domain": "mqtt"}),
        # The server's integration_type filter; Core would refuse an unknown key.
        (c.WS_CONFIG_ENTRIES, {"type_filter": ["hub"]}),
        (c.WS_REGISTRY_LOOKUP, {"entity_ids": ["light.a"]}),
        (c.WS_REGISTRY_LOOKUP, {"config_entry_id": "cfg1"}),
        (c.WS_ENTITY_LOOKUP, {"unique_id": "abc", "platform": "zha"}),
        (
            c.WS_REGISTRIES,
            {"registries": ["area", "category"], "category_scopes": ["automation"]},
        ),
    ],
)
async def test_well_formed_parameters_are_accepted(
    component_ws: MockHAClientWebSocket, command: str, params: dict[str, Any]
) -> None:
    reply = await ws_call(component_ws, command, **params)

    assert reply["success"] is True, reply


@pytest.mark.parametrize(
    ("command", "params"),
    [
        (c.WS_SEARCH, {"limit": 0}),
        (c.WS_SEARCH, {"offset": -1}),
        (c.WS_SEARCH, {"search_types": ["bogus"]}),
        (c.WS_SEARCH, {"exact": "yes"}),
        (c.WS_SEARCH, {"result_fields": ["hue_type"]}),
        (c.WS_OVERVIEW, {"include_notifications": "yes"}),
        (c.WS_HELPERS_LIST, {"helper_types": "template"}),
        (c.WS_HELPERS_LIST, {"include_flow_helpers": "yes"}),
        (c.WS_STATES, {}),
        (c.WS_STATES, {"entity_ids": "light.a"}),
        (c.WS_STATES, {"entity_ids": [1, 2]}),
        (c.WS_BLUEPRINT_GET, {"path": "x.yaml"}),
        (c.WS_BLUEPRINT_GET, {"domain": "automation"}),
        (c.WS_BLUEPRINT_GET, {"domain": "scene", "path": "x.yaml"}),
        (c.WS_DEVICE_GET, {}),
        (c.WS_DEVICE_GET, {"device_id": 5}),
        (c.WS_DEVICE_GET, {"device_id": "d1", "include_entities": "yes"}),
        (c.WS_DEVICE_LIST, {"area_id": "x"}),
        (c.WS_CONFIG_ENTRIES, {"entry_id": 5}),
        (c.WS_CONFIG_ENTRIES, {"domain": 5}),
        (c.WS_CONFIG_ENTRIES, {"bogus": "x"}),
        (c.WS_CONFIG_ENTRIES, {"type_filter": "hub"}),
        # Exclusive: both targets at once.
        (c.WS_REGISTRY_LOOKUP, {"entity_ids": ["light.a"], "config_entry_id": "c1"}),
        (c.WS_REGISTRY_LOOKUP, {"entity_ids": "light.a"}),
        (c.WS_REGISTRY_LOOKUP, {"config_entry_id": 5}),
        (c.WS_SYSTEM_SNAPSHOT, {"include_states": "yes"}),
        (c.WS_ENTITY_LOOKUP, {}),
        (c.WS_ENTITY_LOOKUP, {"unique_id": 5}),
        (c.WS_ENTITY_LOOKUP, {"unique_id": "a", "domain": 5}),
        (c.WS_REGISTRIES, {}),
        (c.WS_REGISTRIES, {"registries": ["bogus"]}),
        (c.WS_REGISTRIES, {"registries": "area"}),
        (c.WS_REGISTRIES, {"registries": ["area"], "category_scopes": "automation"}),
    ],
)
async def test_malformed_parameters_are_refused_before_the_handler(
    component_ws: MockHAClientWebSocket, command: str, params: dict[str, Any]
) -> None:
    reply = await ws_call(component_ws, command, **params)

    assert reply["success"] is False
    assert reply["error"]["code"] == "invalid_format"
