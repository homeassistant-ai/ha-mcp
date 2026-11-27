"""What a helper or integration form says, from Home Assistant itself (#3217).

Every form, string and catalog row below is Core's: the server's client talks
to a real ``hass``. On Home Assistant 2026.12 the form's texts come from
Core's own flow formatter (``llm.flow.format_result``), read through the
``ha_mcp_tools`` component; the field details are the ones Core sends with
the form, on every version.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaConfigFlowHandler,
    SchemaFlowFormStep,
)
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._component_core import ws_result
from ._core_setup import install_custom_helper, with_config_component
from ._mock_flow_integration import answer_translations, mock_flow_integration

# Remove once the hacs.json minimum is 2026.12.
needs_formatter = pytest.mark.skipif(
    (MAJOR_VERSION, MINOR_VERSION) < (2026, 12),
    reason="Core added llm.flow.format_result in 2026.12",
)


async def _serving_flows(
    hass: HomeAssistant, client: HomeAssistantClient
) -> HomeAssistantClient:
    """``client`` on a ``hass`` that serves flows and their strings (the
    frontend cannot be set up here, so its translations command is registered
    on its own)."""
    websocket_api.async_register_command(hass, websocket_get_translations)
    return await with_config_component(hass, client)


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The component's client."""
    return await _serving_flows(hass, component_core_client)


@pytest.fixture
async def core_only(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The client without the component."""
    return await _serving_flows(hass, core_client)


def _by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in fields}


@needs_formatter
async def test_a_section_carries_the_name_home_assistant_gives_it(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The template sensor form folds its availability and attributes into a
    section; Core's formatter names it, so the agent knows what it holds."""
    result = await HelperConfigTools(client).ha_config_list_helpers(
        "template", describe=True, menu_choice="sensor"
    )

    strings = await async_get_translations(hass, "en", "config", ["template"])
    key = "component.template.config.step.sensor.sections.additional_options.name"
    assert _by_name(result["fields"])["additional_options"]["label"] == strings[key]


@needs_formatter
async def test_a_later_form_carries_core_s_label_and_help(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A form the flow shows after the first one is described up front with
    the label and help text Core gives each of its fields."""
    result = await HelperConfigTools(client).ha_config_list_helpers(
        "statistics", describe=True
    )

    strings = await async_get_translations(hass, "en", "config", ["statistics"])
    step = "component.statistics.config.step.options"
    options = next(f for f in result["later_forms"] if f["step_id"] == "options")
    precision = _by_name(options["fields"])["precision"]
    assert precision["label"] == strings[f"{step}.data.precision"]
    assert precision["description"] == strings[f"{step}.data_description.precision"]


async def test_every_selector_setting_core_sends_reaches_the_description(
    hass: HomeAssistant, core_only: HomeAssistantClient
) -> None:
    """A number's zero minimum, a select's option labels and every other
    setting of a field's selector are described as Core sends them, not
    filtered through a list of keys kept by hand."""
    result = await HelperConfigTools(core_only).ha_config_list_helpers(
        "derivative", describe=True
    )

    step = await hass.config_entries.flow.async_init(
        "derivative", context={"source": "user"}
    )
    hass.config_entries.flow.async_abort(step["flow_id"])
    sent = probatio.to_field_list(
        step["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert isinstance(sent, list)
    described = _by_name(result["fields"])
    for field in sent:
        ((kind, config),) = field["selector"].items()
        ours = described[field["name"]]
        assert ours["type"] == kind
        for key, value in config.items():
            if value is not None:
                assert ours[key] == value, (field["name"], key)
    assert described["round"]["min"] == 0


async def test_an_existing_helper_s_entity_choices_are_described(
    hass: HomeAssistant, core_only: HomeAssistantClient
) -> None:
    """Editing a derivative helper offers only the entities its options form
    lists; the description carries that list."""
    hass.states.async_set("sensor.energy", "1", {"unit_of_measurement": "kWh"})
    entry = await hass.config_entries.flow.async_init(
        "derivative", context={"source": "user"}
    )
    created = await hass.config_entries.flow.async_configure(
        entry["flow_id"],
        {
            "name": "Energy rate",
            "source": "sensor.energy",
            "round": 2,
            "time_window": {"minutes": 5},
            "unit_time": "h",
        },
    )
    await hass.async_block_till_done()

    result = await HelperConfigTools(core_only).ha_config_list_helpers(
        "derivative", describe=True, helper_id=created["result"].entry_id
    )

    assert "sensor.energy" in _by_name(result["fields"])["source"]["include_entities"]


# --- a later form's password ---------------------------------------------------------

_LATER_FORM_HELPER = "flow_format_later_form_probe"


class _LaterFormFlow(SchemaConfigFlowHandler):
    """A helper whose second form suggests a stored password."""

    config_flow: Mapping[str, SchemaFlowFormStep] = {
        "user": SchemaFlowFormStep(
            probatio.Schema({probatio.Required("name"): TextSelector()}),
            next_step="extra",
        ),
        "extra": SchemaFlowFormStep(
            probatio.Schema(
                {
                    probatio.Optional(
                        "api_key", description={"suggested_value": "hunter2"}
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
                }
            )
        ),
    }

    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        return "probe"


@pytest.mark.usefixtures("redact_on")
async def test_a_later_forms_suggested_password_is_redacted(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> None:
    """Core's formatter lifts a field's suggested value out of its
    description; a password's stays withheld in the later forms either way."""
    install_custom_helper(tmp_path, monkeypatch, _LATER_FORM_HELPER)
    mock_integration(hass, MockModule(_LATER_FORM_HELPER))
    mock_platform(hass, f"{_LATER_FORM_HELPER}.config_flow", None)

    with mock_config_flow(_LATER_FORM_HELPER, _LaterFormFlow):
        result = await HelperConfigTools(client).ha_config_list_helpers(
            _LATER_FORM_HELPER, describe=True
        )

    (later,) = result["later_forms"]
    assert later["step_id"] == "extra"
    assert "hunter2" not in str(result)
    assert _by_name(later["fields"])["api_key"]["current"].startswith("<redacted")


# --- what the component formats ----------------------------------------------------


async def _format(component_ws: Any, manager: str, **step: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(
        component_ws, "ha_mcp_tools/flow_format", manager=manager, step=step
    )
    return result


@needs_formatter
async def test_a_form_is_formatted_only_while_the_flow_shows_it_unchanged(
    hass: HomeAssistant, component_ws: Any
) -> None:
    """The form is read from the flow itself, so a step the flow has left,
    changed into another kind of step or answered again with other errors is
    not formatted, and nothing is run to read it."""
    flow = await hass.config_entries.flow.async_init(
        "derivative", context={"source": "user"}
    )
    shown = {"flow_id": flow["flow_id"], "handler": "derivative"}

    as_shown = await _format(
        component_ws, "config", **shown, type="form", step_id="user"
    )
    others = [
        await _format(component_ws, "config", **shown, **changed)
        for changed in (
            {"type": "form", "step_id": "options"},
            {"type": "menu", "step_id": "user"},
            {"type": "form", "step_id": "user", "errors": {"base": "unknown"}},
            {"type": "no_such_type", "step_id": "user"},
        )
    ]

    assert as_shown["found"] is True
    assert as_shown["step"]["step_id"] == "user"
    assert [o["found"] for o in others] == [False] * len(others)
    assert hass.config_entries.flow.async_get(flow["flow_id"])["step_id"] == "user"
    hass.config_entries.flow.async_abort(flow["flow_id"])


@needs_formatter
async def test_a_flow_of_an_entry_core_does_not_hold_is_not_formatted(
    component_ws: Any,
) -> None:
    result = await _format(
        component_ws,
        "options",
        flow_id="gone",
        handler="no_such_entry",
        type="abort",
        reason="unknown",
    )

    assert result == {"supported": True, "found": False}


@needs_formatter
async def test_an_options_abort_without_its_own_text_gets_home_assistants(
    hass: HomeAssistant, component_ws: Any
) -> None:
    """Core's formatter falls back on Home Assistant's common abort texts,
    which live under the config strings, for an options flow too."""
    entry = MockConfigEntry(domain="derivative")
    entry.add_to_hass(hass)
    common = await async_get_translations(hass, "en", "config", ["homeassistant"])
    own = await async_get_translations(hass, "en", "options", ["derivative"])
    prefix = "component.homeassistant.config.abort."
    reason, text = next(
        (key.removeprefix(prefix), value)
        for key, value in common.items()
        if key.startswith(prefix)
        and f"component.derivative.options.abort.{key.removeprefix(prefix)}" not in own
    )

    result = await _format(
        component_ws,
        "options",
        flow_id="ended",
        handler=entry.entry_id,
        type="abort",
        reason=reason,
    )

    assert result["step"]["description"] == text


# --- Core's language ---------------------------------------------------------------

_LANGUAGE_PROBE = "flow_format_language_probe"


def _language_strings(title: str, submit: str, unit_a: str) -> dict[str, Any]:
    return {
        "config": {"step": {"user": {"title": title, "submit": submit}}},
        "selector": {"unit": {"options": {"a": unit_a}}},
    }


class _LanguageFlow(ConfigFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required("unit"): SelectSelector(
                        SelectSelectorConfig(options=["a", "b"], translation_key="unit")
                    )
                }
            ),
        )


async def test_a_flows_texts_are_in_core_s_language(
    hass: HomeAssistant,
    flow_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a German Home Assistant the texts the tool reads itself (the
    submit label, a select's option labels) are German too, beside what
    Core's formatter gives, not English."""
    answer_translations(hass)
    english = _language_strings("Connect", "Save", "Amps")
    mock_flow_integration(hass, _LANGUAGE_PROBE, tmp_path, monkeypatch, english)
    folder = (
        tmp_path / "homeassistant" / "components" / _LANGUAGE_PROBE / "translations"
    )
    german = _language_strings("Verbinden", "Speichern", "Ampere")
    (folder / "de.json").write_text(json.dumps(german), encoding="utf-8")
    await hass.config.async_update(language="de")
    tools = IntegrationTools(await with_config_component(hass, flow_client))

    with mock_config_flow(_LANGUAGE_PROBE, _LanguageFlow):
        result = await tools.ha_set_integration(domain=_LANGUAGE_PROBE)

    assert (result["title"], result["submit"]) == ("Verbinden", "Speichern")
    assert _by_name(result["fields"])["unit"]["option_labels"] == {"a": "Ampere"}
    await tools.ha_set_integration(
        domain=_LANGUAGE_PROBE, flow_id=result["flow_id"], abort=True
    )
