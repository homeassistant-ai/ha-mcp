"""What Home Assistant can create, as ha_config_list_helpers(catalog=True) and
ha_get_integration(catalog=True) list it (#3217).

Every row is Core's: the server's client talks to a real ``hass`` and the
catalog is Core's ``integration/descriptions``, custom integrations included.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant import generated
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration_descriptions

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._core_setup import install_custom_helper, with_config_component
from ._tool_errors import rejected_call_body


@pytest.fixture
async def helpers(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HelperConfigTools:
    websocket_api.async_register_command(hass, websocket_get_translations)
    return HelperConfigTools(await with_config_component(hass, core_client))


@pytest.fixture
async def integrations(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> IntegrationTools:
    websocket_api.async_register_command(hass, websocket_get_translations)
    return IntegrationTools(await with_config_component(hass, core_client))


def _install_custom_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str, name: str
) -> None:
    """A custom integration of ``domain`` where Core's loader looks."""
    folder = tmp_path / "extra" / "custom_components" / domain
    folder.mkdir(parents=True)
    manifest = {
        "domain": domain,
        "name": name,
        "config_flow": True,
        "integration_type": "hub",
        "version": "1.0.0",
        "documentation": "https://example.invalid",
        "codeowners": [],
        "requirements": [],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.syspath_prepend(str(tmp_path / "extra"))


async def _core_integrations(hass: HomeAssistant) -> dict[str, Any]:
    integrations: dict[str, Any] = (await async_get_integration_descriptions(hass))[
        "core"
    ]["integration"]
    return integrations


# --- helper types ----------------------------------------------------------------


async def test_the_helper_catalog_lists_what_core_can_create(
    hass: HomeAssistant,
    helpers: HelperConfigTools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> None:
    """Every helper type Core's Create helper catalog offers, a custom one
    included, with its categories as Core sends them."""
    install_custom_helper(tmp_path, monkeypatch, "catalog_probe_helper")

    rows = {
        row["helper_type"]: row
        for row in (await helpers.ha_config_list_helpers("all", catalog=True))[
            "catalog"
        ]
    }

    core = (await async_get_integration_descriptions(hass))["core"]["helper"]
    assert "catalog_probe_helper" in rows
    assert rows["derivative"]["categories"] == core["derivative"]["categories"]
    assert rows["derivative"]["config_flow"] is True
    assert rows["counter"]["config_flow"] is False


async def test_a_category_keeps_its_subcategories_and_a_subcategory_only_itself(
    hass: HomeAssistant, helpers: HelperConfigTools
) -> None:
    store = await helpers.ha_config_list_helpers(
        "all", catalog=True, category="helpers.store_value"
    )
    every = await helpers.ha_config_list_helpers(
        "all", catalog=True, category="helpers"
    )

    assert store["catalog"]
    for row in store["catalog"]:
        assert "helpers.store_value" in row["categories"]
    assert {r["helper_type"] for r in store["catalog"]} < {
        r["helper_type"] for r in every["catalog"]
    }
    for row in every["catalog"]:
        assert any(c.partition(".")[0] == "helpers" for c in row["categories"])


async def test_a_storage_type_outside_the_dialog_says_it_is_still_created(
    hass: HomeAssistant, helpers: HelperConfigTools
) -> None:
    """A zone is created with ha_config_set_helper, though Core's Create
    helper catalog does not list it: an empty answer says so."""
    listed = (await async_get_integration_descriptions(hass))["core"]["helper"]
    assert "zone" not in listed

    result = await helpers.ha_config_list_helpers("zone", catalog=True)

    assert result["catalog"] == []
    assert any("ha_config_set_helper creates it" in w for w in result["warnings"])


async def test_an_unknown_category_lists_the_categories_there_are(
    helpers: HelperConfigTools,
) -> None:
    result = await helpers.ha_config_list_helpers(
        "all", catalog=True, category="no_such_category"
    )

    assert result["catalog"] == []
    assert "helpers" in result["warnings"][0]


async def test_a_known_category_is_not_called_unknown_when_the_type_empties_it(
    helpers: HelperConfigTools,
) -> None:
    result = await helpers.ha_config_list_helpers(
        "counter", catalog=True, category="helpers.calculate_value"
    )

    assert result["catalog"] == []
    assert not any("No entry has category" in w for w in result.get("warnings", []))


# --- integrations ----------------------------------------------------------------


async def test_the_integration_catalog_lists_only_what_a_config_flow_sets_up(
    hass: HomeAssistant, integrations: IntegrationTools
) -> None:
    core = await _core_integrations(hass)
    yaml_only = next(
        d
        for d, m in core.items()
        if "integration_type" in m and not m.get("config_flow")
    )
    with_flow = next(
        d for d, m in core.items() if "integration_type" in m and m.get("config_flow")
    )

    whole = await integrations.ha_get_integration(catalog=True, limit=5000)
    assert whole["has_more"] is False
    domains = {row["domain"] for row in whole["catalog"]}

    assert with_flow in domains
    assert yaml_only not in domains


async def test_a_page_is_the_next_slice_of_the_whole_list(
    integrations: IntegrationTools,
) -> None:
    whole = await integrations.ha_get_integration(catalog=True, limit=6)
    page = await integrations.ha_get_integration(catalog=True, limit=2, offset=2)

    assert page["catalog"] == whole["catalog"][2:4]
    assert page["has_more"] is True


async def test_an_integration_is_found_by_its_name_as_core_names_it(
    hass: HomeAssistant, integrations: IntegrationTools
) -> None:
    """A name Core translates comes from its title strings."""
    core = await _core_integrations(hass)
    translated = (await async_get_integration_descriptions(hass))["core"][
        "translated_name"
    ]
    domain = next(d for d in translated if core.get(d, {}).get("config_flow"))
    titles = await async_get_translations(hass, "en", "title", [domain])
    name = titles[f"component.{domain}.title"]

    found = await integrations.ha_get_integration(catalog=True, query=name)

    assert {"domain": domain, "name": name} in [
        {"domain": r["domain"], "name": r["name"]} for r in found["catalog"]
    ]


async def test_a_custom_integration_replaces_the_one_a_brand_groups(
    hass: HomeAssistant,
    integrations: IntegrationTools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> None:
    """Core keeps a brand's nested row beside a custom integration of that
    domain; the custom one is what Home Assistant loads and what is listed."""
    # Core's own catalog file: reading it through the loader now would cache
    # the custom integrations before this one is installed.
    core = json.loads((Path(generated.__path__[0]) / "integrations.json").read_text())[
        "integration"
    ]
    nested = next(
        d
        for brand, m in core.items()
        for d, meta in (m.get("integrations") or {}).items()
        if meta.get("config_flow") and d != brand and d not in core
    )
    _install_custom_integration(tmp_path, monkeypatch, nested, "My Fork Of It")

    found = await integrations.ha_get_integration(catalog=True, query="My Fork")

    assert [(r["domain"], r["name"]) for r in found["catalog"]] == [
        (nested, "My Fork Of It")
    ]


# --- parameters --------------------------------------------------------------------


async def test_a_category_without_catalog_is_refused_by_both_tools(
    helpers: HelperConfigTools, integrations: IntegrationTools
) -> None:
    for call in (
        helpers.ha_config_list_helpers("all", category="helpers"),
        integrations.ha_get_integration(category="energy"),
    ):
        body = await rejected_call_body(call)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_entry_parameters_beside_catalog_are_named_as_ignored(
    helpers: HelperConfigTools, integrations: IntegrationTools
) -> None:
    helper = await helpers.ha_config_list_helpers(
        "derivative", catalog=True, describe=True
    )
    integration = await integrations.ha_get_integration(
        catalog=True, domain="met", include_options=True, limit=1
    )

    assert any("describe" in w for w in helper["warnings"])
    assert any(
        "domain" in w and "include_options" in w for w in integration["warnings"]
    )
