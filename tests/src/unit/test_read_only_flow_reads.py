"""Read Only Mode: the integration reads still open the options flow (first form only).

An integration with an options flow and a subentry flow is registered in a real
``hass``; the tools read it through ``core_client``, so every flow form and
abort is Core's own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import read_only_request
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

_DOMAIN = "readonly_flow_reads"


def _host_schema() -> probatio.Schema:
    return probatio.Schema({probatio.Optional("host"): selector.TextSelector()})


class _SubentryFlow(ConfigSubentryFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_form(step_id="user", data_schema=_host_schema())


class _OptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _host_schema(), self.config_entry.options
            ),
        )


class _ConfigFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _OptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"conversation": _SubentryFlow}


@pytest.fixture
async def entry(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[MockConfigEntry]:
    """A config entry whose integration has an options and a subentry flow."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "logger", {})
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow")
    config_entry = MockConfigEntry(domain=_DOMAIN, options={"host": "h"})
    config_entry.add_to_hass(hass)
    with mock_config_flow(_DOMAIN, _ConfigFlow):
        yield config_entry


@pytest.mark.parametrize(
    ("call", "read_key"),
    [
        ({}, None),
        ({"include_schema": True}, "options_schema"),
        (
            {"include_subentry_schema": True, "subentry_type": "conversation"},
            "subentry_schema",
        ),
    ],
    ids=["entry", "options-first-form", "subentry-first-form"],
)
async def test_read_only_runs_the_entry_reads_and_first_form_describes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entry: MockConfigEntry,
    call: dict[str, Any],
    read_key: str | None,
) -> None:
    tools = IntegrationTools(core_client)

    with read_only_request():
        result = await tools.ha_get_integration(entry_id=entry.entry_id, **call)

    assert result["success"] is True
    assert result["entry"]["entry_id"] == entry.entry_id
    if read_key is not None:
        assert result[read_key]
    assert hass.config_entries.options.async_progress() == []
    assert hass.config_entries.subentries.async_progress() == []


async def test_read_only_runs_the_domain_list_read(
    core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    tools = IntegrationTools(core_client)

    with read_only_request():
        result = await tools.ha_get_integration(domain=_DOMAIN)

    assert result["success"] is True
    assert [row["entry_id"] for row in result["entries"]] == [entry.entry_id]


async def test_read_only_options_probe_reads_the_options_flow(
    hass: HomeAssistant, core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    with read_only_request():
        options, read = await fetch_entry_options_with_status(
            core_client, entry.entry_id
        )

    assert (options, read) == ({"host": "h"}, True)
    assert hass.config_entries.options.async_progress() == []


async def test_read_only_is_not_named_as_why_a_helper_search_is_partial(
    core_client: HomeAssistantClient,
) -> None:
    # No input_* helper integration is set up, so Core answers each list with
    # an error and the helper backends count as not scanned.
    tools = SmartSearchTools(client=core_client, fuzzy_threshold=60)

    with read_only_request():
        response = await tools.deep_search("kitchen", search_types=["helper"])

    assert "OptionsFlow could not be read" in response["partial_reason"]
