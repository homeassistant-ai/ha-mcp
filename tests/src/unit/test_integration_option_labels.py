"""The labels the HA UI shows for a select's options in a described flow form.

Core labels a select's options under its ``translation_key`` in the
integration's ``selector`` strings. The form is a mock integration's options
form, run by Core's flow manager through ``ha_get_integration`` on a real
``hass``; Core's own ``frontend/get_translations`` reads the strings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._command_faults import pooled_socket
from ._mock_flow_integration import answer_translations, mock_flow_integration
from ._ws_transport import abort_transport

_DOMAIN = "label_demo"
_STRINGS = {
    "selector": {
        "day_level": {"options": {"low": "Low light", "high": "High light"}},
        "night_level": {"options": {"dim": "Dimmed", "off": "Off"}},
    }
}
TRANSLATIONS = "frontend/get_translations"


def _select(options: list[str], key: str) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=options, translation_key=key))


# A select of one name in two sections, and a text field of that name too.
_SCHEMA = vol.Schema(
    {
        vol.Required("level"): TextSelector(),
        vol.Required("day"): section(
            vol.Schema({vol.Required("level"): _select(["low", "high"], "day_level")}),
            {"collapsed": False},
        ),
        vol.Required("night"): section(
            vol.Schema({vol.Required("level"): _select(["dim", "off"], "night_level")}),
            {"collapsed": False},
        ),
    }
)


class _Options(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="init", data_schema=_SCHEMA)


class _Flow(ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _Options()


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[MockConfigEntry]:
    """An entry of the mock integration whose options form is ``_SCHEMA``."""
    assert await async_setup_component(hass, "config", {})
    mock_flow_integration(hass, _DOMAIN, tmp_path, monkeypatch, _STRINGS)
    answer_translations(hass)
    with mock_config_flow(_DOMAIN, _Flow):
        added = MockConfigEntry(domain=_DOMAIN)
        added.add_to_hass(hass)
        yield added


async def _labels(hass: HomeAssistant, key: str, options: list[str]) -> dict[str, str]:
    texts = await async_get_translations(hass, "en", "selector", [_DOMAIN])
    prefix = f"component.{_DOMAIN}.selector.{key}.options"
    return {option: texts[f"{prefix}.{option}"] for option in options}


async def test_each_section_keeps_its_own_selects_labels(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    """Two sections may hold a select of the same name with other options;
    each gets its own labels, and a field of that name that is no select
    gets none."""
    resp = await IntegrationTools(core_client).ha_get_integration(
        entry_id=entry.entry_id, include_schema=True
    )

    fields = {f["name"]: f for f in resp["options_schema"]["fields"]}
    [day] = fields["day"]["fields"]
    [night] = fields["night"]["fields"]
    assert day["option_labels"] == await _labels(hass, "day_level", ["low", "high"])
    assert night["option_labels"] == await _labels(hass, "night_level", ["dim", "off"])
    assert "option_labels" not in fields["level"]


async def test_a_failed_label_read_is_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    """The help-text read of the same form warns when it fails; the labels'
    read, from the same strings, must not fail silently into no labels."""
    socket = await pooled_socket(core_client)
    core_handler, schema = hass.data[websocket_api.DOMAIN][TRANSLATIONS]

    def drop_selector_read(
        core: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        if msg["category"] == "selector":
            abort_transport(socket)
            return
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, TRANSLATIONS, drop_selector_read, schema)
    try:
        resp = await IntegrationTools(core_client).ha_get_integration(
            entry_id=entry.entry_id, include_schema=True
        )
    finally:
        websocket_api.async_register_command(hass, TRANSLATIONS, core_handler, schema)

    fields = {f["name"]: f for f in resp["options_schema"]["fields"]}
    assert "option_labels" not in fields["day"]["fields"][0]
    assert any(
        f"strings for {_DOMAIN} could not be read" in w for w in resp["warnings"]
    )
