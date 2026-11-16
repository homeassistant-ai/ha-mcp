"""``ha_search`` over UI-created flow helpers (template, group, ...), issue #1457.

Flow helpers are config entries, not storage records: the search lists the
entries of every helper flow Home Assistant lists and reads each one's current
config from its options flow, so a template body or group members are
searchable. Only Home Assistant's own helper flows are opened (#2987).

Every test searches a real ``hass`` through the server's own client; the
helper flow lists are Core's own answers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.config import config_entries as config_entries_view
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.tools.smart_search import SmartSearchTools

from ._core_setup import install_custom_helper
from ._template_helper_core import create_template_sensor

CUSTOM = "my_custom_helper"
_STORAGE_HELPERS = (
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "input_button",
)
BACKEND_NOT_SCANNED = "1 helper backend(s) not scanned"
CUSTOM_NOT_SCANNED = "1 custom helper integration entry not scanned"


@pytest.fixture
def _server_helper_flows() -> None:
    """The server reads Core's helper flow lists itself."""


@pytest.fixture
async def storage_helpers(hass: HomeAssistant) -> None:
    """The ``input_*`` lists answer, so only the flow helpers can make a
    search partial."""
    for domain in _STORAGE_HELPERS:
        assert await async_setup_component(hass, domain, {})


@pytest.fixture
async def helpers_up(hass: HomeAssistant, storage_helpers: None) -> None:
    """``config`` serves the entry and helper flow lists; ``template`` is
    loaded, so Core knows its entries have an options flow."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "template", {})


async def template_helper(hass: HomeAssistant, title: str, state: str) -> ConfigEntry:
    """A template sensor helper titled ``title`` whose state template is ``state``."""
    entry = await create_template_sensor(hass, "0")
    hass.config_entries.async_update_entry(
        entry, title=title, options={**entry.options, "state": state}
    )
    await hass.async_block_till_done()
    return entry


def unknown_type_template(hass: HomeAssistant, title: str) -> MockConfigEntry:
    """A stored template helper of a type this Core does not know (as a newer
    Home Assistant would have made it): its options flow cannot be read."""
    entry = MockConfigEntry(
        domain="template",
        title=title,
        options={"name": title, "template_type": "no_such_type", "state": "{{ 1 }}"},
    )
    entry.add_to_hass(hass)
    return entry


async def search(
    client: HomeAssistantClient,
    query: str,
    *,
    include_config: bool = False,
    exact_match: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = await SmartSearchTools(client=client).deep_search(
        query,
        search_types=["helper"],
        limit=10,
        include_config=include_config,
        exact_match=exact_match,
    )
    return result


async def search_with_config(
    client: HomeAssistantClient, query: str
) -> tuple[dict[str, Any], list[str]]:
    """Search with configs, and the entries whose options flow it opened (Core
    still answers each one)."""
    with patch.object(
        client, "start_options_flow", wraps=client.start_options_flow
    ) as opened:
        result = await search(client, query, include_config=True)
    return result, [call.args[0] for call in opened.await_args_list]


@pytest.mark.usefixtures("helpers_up")
async def test_a_flow_helper_is_found_by_its_title(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    weather = await template_helper(hass, "Weather Message", "{{ 1 }}")
    await template_helper(hass, "Unrelated", "{{ 2 }}")

    result = await search(core_client, "weather")

    [match] = result["helpers"]
    assert match["entry_id"] == weather.entry_id
    assert match["helper_type"] == "template"
    assert match["name"] == "Weather Message"
    assert match["match_in_name"] is True
    assert "config" not in match
    assert "partial" not in result


@pytest.mark.usefixtures("helpers_up")
async def test_a_flow_helper_is_found_by_its_template_and_its_flow_is_closed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The template body is read from the options flow the UI opens; the
    search leaves no options flow open behind it."""
    entry = await template_helper(
        hass, "Renamed Sensor", "{{ states('sensor.outside_temperature') | float }}"
    )

    result = await search(core_client, "outside_temperature")

    [match] = result["helpers"]
    assert match["entry_id"] == entry.entry_id
    assert match["match_in_config"] is True
    assert hass.config_entries.options.async_progress() == []


@pytest.mark.usefixtures("helpers_up")
async def test_include_config_returns_the_helpers_current_template(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The title alone already matches; the body is still read to return it."""
    await template_helper(hass, "Comfort Index", "{{ states('sensor.comfort') }}")

    result = await search(core_client, "comfort", include_config=True)

    [match] = result["helpers"]
    assert match["config"]["state"] == "{{ states('sensor.comfort') }}"


@pytest.mark.usefixtures("helpers_up")
async def test_another_integrations_entry_is_not_a_helper_result(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    MockConfigEntry(domain="hue", title="Match Hue Bridge").add_to_hass(hass)
    helper = await template_helper(hass, "Match Me", "{{ true }}")

    result = await search(core_client, "match")

    assert [m["entry_id"] for m in result["helpers"]] == [helper.entry_id]


@pytest.mark.usefixtures("helpers_up")
async def test_a_helper_without_an_options_flow_is_found_by_its_title(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """OTP has no options flow, so there is no body to read: the entry is
    found by its title, with an empty config, and the search is complete."""
    entry = MockConfigEntry(domain="otp", title="Router Login")
    entry.add_to_hass(hass)

    result, opened = await search_with_config(core_client, "router")

    assert [(m["entry_id"], m["config"]) for m in result["helpers"]] == [
        (entry.entry_id, {})
    ]
    assert "partial" not in result
    assert opened == []


@pytest.mark.usefixtures("storage_helpers")
async def test_an_unreadable_entry_list_makes_the_search_partial(
    core_client: HomeAssistantClient,
) -> None:
    """Without ``config`` Core serves no entry list: the flow helpers were not
    searched, which the search says instead of reporting no helpers."""
    result = await search(core_client, "anything")

    assert result["helpers"] == []
    assert result["partial"] is True
    assert BACKEND_NOT_SCANNED in result["partial_reason"]


@pytest.mark.usefixtures("helpers_up")
async def test_no_flow_helpers_is_a_complete_answer(
    core_client: HomeAssistantClient,
) -> None:
    result = await search(core_client, "anything")

    assert result["helpers"] == []
    assert "partial" not in result


@pytest.mark.usefixtures("helpers_up")
async def test_a_flow_helper_is_not_found_by_its_opaque_entry_id(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Part of the entry's random id is no match (issue #1457 review); its
    title still is."""
    entry = await template_helper(hass, "Bedroom Light", "{{ 1 + 1 }}")

    by_id = await search(core_client, entry.entry_id[-8:].lower())
    by_title = await search(core_client, "bedroom")

    assert by_id["helpers"] == []
    assert [m["entry_id"] for m in by_title["helpers"]] == [entry.entry_id]


@pytest.mark.usefixtures("helpers_up")
async def test_an_unreadable_helper_body_is_reported_and_the_others_still_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    unknown_type_template(hass, "Broken")
    good = await template_helper(
        hass, "Renamed Sensor", "{{ states('sensor.outside_temperature') }}"
    )

    result = await search(core_client, "outside_temperature")

    assert [m["entry_id"] for m in result["helpers"]] == [good.entry_id]
    assert BACKEND_NOT_SCANNED in result["partial_reason"]


@pytest.mark.usefixtures("helpers_up")
async def test_a_title_match_with_an_unreadable_body_still_makes_the_search_partial(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The entry is returned on its title, but its body was never searched."""
    entry = unknown_type_template(hass, "Kitchen Temp")

    result = await search(core_client, "kitchen", include_config=True)

    assert [m["entry_id"] for m in result["helpers"]] == [entry.entry_id]
    assert BACKEND_NOT_SCANNED in result["partial_reason"]


@pytest.mark.usefixtures("helpers_up")
async def test_a_fuzzy_title_match_below_a_perfect_score_still_has_its_body_searched(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title that matches but scores below 100 does not stop the body read:
    the body can score higher, and results are ranked by score."""
    monkeypatch.setattr(get_global_settings(), "fuzzy_threshold", 60)
    await template_helper(
        hass, "Garden Climate", "{{ states('sensor.garden_humidity') }}"
    )

    result = await search(core_client, "garden humidity", exact_match=False)

    [match] = result["helpers"]
    assert match["match_in_name"] is True
    assert match["match_in_config"] is True


class _CustomHelperFlow(ConfigFlow):
    """A custom helper integration's flow, with an options flow of its own."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlow()


@pytest.fixture
def custom_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_custom_integrations: None
) -> Iterator[None]:
    """``my_custom_helper``: a helper flow Core lists that is not Core's own."""
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    with mock_config_flow(CUSTOM, _CustomHelperFlow):
        yield


@pytest.mark.usefixtures("helpers_up", "custom_helper")
async def test_a_search_opens_no_options_flow_for_a_custom_helper_integration(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the component, a helper flow outside Home Assistant's own is not
    opened (#2987): its first step is the integration's own code. The entry is
    still found by its title, its config is withheld, and the search says its
    body was not searched. Core's own template helper beside it is read."""
    custom = MockConfigEntry(domain=CUSTOM, title="Gate Opener")
    custom.add_to_hass(hass)
    template = await template_helper(hass, "Gate Template", "{{ 1 }}")

    result, opened = await search_with_config(core_client, "gate")

    [found] = [m for m in result["helpers"] if m["entry_id"] == custom.entry_id]
    assert found["config"] is None
    assert found["options_withheld"] == "custom_integration"
    assert CUSTOM_NOT_SCANNED in result["partial_reason"]
    assert opened == [template.entry_id]


@pytest.mark.usefixtures("helpers_up")
async def test_a_non_admin_token_opens_no_flow_and_says_the_bodies_were_not_searched(
    hass: HomeAssistant,
    read_only_client: HomeAssistantClient,
) -> None:
    """Core's catalog of its own helpers is admin-only: without it no entry
    can be told apart from a custom one, so none is opened, and each counts
    as unsearched."""
    entry = await template_helper(hass, "Anything", "{{ 1 }}")

    result, opened = await search_with_config(read_only_client, "anything")

    assert opened == []
    assert [m["entry_id"] for m in result["helpers"]] == [entry.entry_id]
    assert CUSTOM_NOT_SCANNED in result["partial_reason"]


@pytest.mark.usefixtures("helpers_up")
async def test_a_failed_helper_flow_list_after_the_entries_is_one_failure(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entries read succeeds and Core fails the helper flow list: the
    flow helpers count as one unscanned backend instead of failing the search."""
    await template_helper(hass, "Anything", "{{ 1 }}")
    monkeypatch.setattr(
        config_entries_view,
        "async_get_config_flows",
        AsyncMock(side_effect=HomeAssistantError("flow list unavailable")),
    )

    result = await search(core_client, "anything")

    assert result["helpers"] == []
    assert BACKEND_NOT_SCANNED in result["partial_reason"]
