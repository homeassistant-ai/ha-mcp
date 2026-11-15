"""Start the HA-MCP config flow and options flows in Core's flow managers.

Every step runs through ``hass.config_entries.flow`` / ``hass.config_entries.options``,
so Core validates the submitted input against the schema the step showed and
builds each result as it does for a user.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    SERVER_ENTRY_TITLE,
    TOOLS_ENTRY_TITLE,
)

FlowResultDict = FlowResult[Any, Any]

COMPONENT_DIR = Path(custom_components.ha_mcp_tools.__file__).parent
MANIFEST_VERSION: str = json.loads(
    (COMPONENT_DIR / "manifest.json").read_text("utf-8")
)["version"]
STRINGS_COMMON: dict[str, str] = json.loads(
    (COMPONENT_DIR / "strings.json").read_text("utf-8")
)["common"]
GERMAN_COMMON: dict[str, str] = json.loads(
    (COMPONENT_DIR / "translations" / "de.json").read_text("utf-8")
)["common"]


async def start_config_flow(hass: HomeAssistant) -> FlowResultDict:
    """Open the integration's setup flow as a user does: the entry-type menu."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def choose_entry_type(
    hass: HomeAssistant, flow_id: str, entry_type: str
) -> FlowResultDict:
    """Pick ``entry_type`` (``server`` or ``tools``) in the menu."""
    return await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": entry_type}
    )


async def start_entry_type_flow(hass: HomeAssistant, entry_type: str) -> FlowResultDict:
    """Open the setup flow and pick ``entry_type``: the first form of that branch."""
    menu = await start_config_flow(hass)
    return await choose_entry_type(hass, menu["flow_id"], entry_type)


async def submit_config_step(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]
) -> FlowResultDict:
    """Submit ``user_input`` to the setup flow's open step."""
    return await hass.config_entries.flow.async_configure(flow_id, user_input)


async def start_options_flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> FlowResultDict:
    """Open the entry's options flow: its first form."""
    return await hass.config_entries.options.async_init(entry.entry_id)


async def submit_options_step(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]
) -> FlowResultDict:
    """Submit ``user_input`` to the options flow's open step."""
    return await hass.config_entries.options.async_configure(flow_id, user_input)


async def add_tools_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    loaded: bool = False,
) -> MockConfigEntry:
    """A File & YAML Tools entry, set up by Core when ``loaded``.

    ``data`` defaults to the entry type marker; ``{}`` is the shape older
    installs stored.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS} if data is None else data,
    )
    entry.add_to_hass(hass)
    if loaded:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def placeholders(result: FlowResultDict) -> dict[str, str]:
    """The description placeholders of a form."""
    shown = result["description_placeholders"]
    assert shown is not None
    return dict(shown)


WEBHOOK_ID = "mcp_abc"


def add_options_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """A server entry holding exactly ``data`` (default: a webhook id) and
    ``options``, not set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SERVER_ENTRY_TITLE,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER,
            **({DATA_WEBHOOK_ID: WEBHOOK_ID} if data is None else data),
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def open_options_form(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> FlowResultDict:
    """The server options form of an entry holding ``data`` and ``options``."""
    return await start_options_flow(
        hass, add_options_entry(hass, data=data, options=options)
    )


def schema_markers(result: FlowResultDict) -> dict[str, vol.Marker]:
    """The form's fields, keyed by field name."""
    schema = result["data_schema"]
    assert schema is not None
    return {str(marker.schema): marker for marker in schema.schema}


def schema_validators(result: FlowResultDict) -> dict[str, Any]:
    """The form's validators (selectors), keyed by field name."""
    schema = result["data_schema"]
    assert schema is not None
    return {str(marker.schema): value for marker, value in schema.schema.items()}


def shown_value(marker: vol.Marker) -> Any:
    """What the form pre-fills for ``marker``: its suggested value, else its default."""
    description = marker.description or {}
    if "suggested_value" in description:
        return description["suggested_value"]
    return marker.default()
