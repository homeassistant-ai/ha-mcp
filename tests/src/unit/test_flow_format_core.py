"""What a helper or integration form says, from Home Assistant itself (#3217).

Every form, string and catalog row below is Core's: the server's client talks
to a real ``hass``. On Home Assistant 2026.12 the form's texts come from
Core's own flow formatter (``llm.flow.format_result``), read through the
``ha_mcp_tools`` component; the field details are the ones Core sends with
the form, on every version.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration_descriptions

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._core_setup import install_custom_helper, with_config_component

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


async def test_the_helper_catalog_lists_what_core_can_create(
    hass: HomeAssistant,
    core_only: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> None:
    """Every helper type Core's Create helper catalog offers, a custom one
    included, with its categories as Core sends them; a category keeps its
    subcategories."""
    install_custom_helper(tmp_path, monkeypatch, "catalog_probe_helper")
    descriptions = await async_get_integration_descriptions(hass)

    tools = HelperConfigTools(core_only)
    catalog = (await tools.ha_config_list_helpers("all", catalog=True))["catalog"]
    stored = (
        await tools.ha_config_list_helpers(
            "all", catalog=True, category="helpers.store_value"
        )
    )["catalog"]

    rows = {row["helper_type"]: row for row in catalog}
    core = descriptions["core"]["helper"]
    assert rows["derivative"]["categories"] == core["derivative"]["categories"]
    assert rows["derivative"]["config_flow"] is True
    assert rows["counter"]["config_flow"] is False
    assert "catalog_probe_helper" in rows
    assert {row["helper_type"] for row in stored} == {
        domain
        for domain, meta in core.items()
        if domain in rows and "helpers.store_value" in meta.get("categories", [])
    }


async def test_an_unknown_category_lists_the_categories_there_are(
    core_only: HomeAssistantClient,
) -> None:
    result = await HelperConfigTools(core_only).ha_config_list_helpers(
        "all", catalog=True, category="no_such_category"
    )

    assert result["catalog"] == []
    assert "helpers" in result["warnings"][0]


async def test_the_integration_catalog_finds_an_integration_by_its_domain(
    hass: HomeAssistant, core_only: HomeAssistantClient
) -> None:
    """Integrations that can be set up are found as Core's own search finds
    them, an exact domain first, named as Core names them, a page at a time."""
    tools = IntegrationTools(core_only)
    found = await tools.ha_get_integration(catalog=True, query="met")
    page = await tools.ha_get_integration(catalog=True, limit=2, offset=2)

    meta = (await async_get_integration_descriptions(hass))["core"]["integration"]
    titles = await async_get_translations(hass, "en", "title", ["met"])
    first = found["catalog"][0]
    assert first["domain"] == "met"
    assert first["name"] == meta["met"].get("name", titles.get("component.met.title"))
    assert first["categories"] == meta["met"].get("categories", [])
    assert len(page["catalog"]) == 2
    assert page["has_more"] is True
