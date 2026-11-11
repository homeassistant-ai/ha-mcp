"""Ignored config entries: no options or reconfigure flow, read or written.

Core never sets an ignored entry up, so its flows can raise, while Core's
``supports_options`` and ``supports_reconfigure`` never look at ``source``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_flow import (
    OptionsFlowError,
    restore_config_entry_options,
)
from ha_mcp.tools.config_entry_identity import check_reconfigure_identity
from ha_mcp.tools.ignored_entries import normalize_ignored_entry
from ha_mcp.tools.integration_flow_steps import options_target

_FLAGS = {"supports_options": True, "supports_reconfigure": True}


@pytest.mark.parametrize(
    ("source", "expected"), [("ignore", False), ("user", True)], ids=["ignored", "user"]
)
def test_only_an_ignored_entry_loses_its_flow_flags(
    source: str, expected: bool
) -> None:
    entry: dict[str, Any] = {"source": source, **_FLAGS}

    normalize_ignored_entry(entry)

    assert entry["supports_options"] is expected
    assert entry["supports_reconfigure"] is expected


_DOMAIN = "ignored_flow_test"


class _OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({vol.Optional("host"): str})
            )
        return self.async_create_entry(data=user_input)


class _ConfigFlow(config_entries.ConfigFlow, domain=_DOMAIN):
    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return _OptionsFlow()


@pytest.fixture
async def flow_domain(hass: HomeAssistant) -> AsyncIterator[None]:
    """A real integration whose config flow offers an options flow."""
    assert await async_setup_component(hass, "config", {})
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow")
    with mock_config_flow(_DOMAIN, _ConfigFlow):
        yield


def _add_entry(hass: HomeAssistant, source: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=_DOMAIN, source=source, title="t")
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("flow_domain")
async def test_reconfigure_of_an_ignored_entry_is_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    entry = _add_entry(hass, config_entries.SOURCE_IGNORE)

    with pytest.raises(ToolError) as exc_info:
        await check_reconfigure_identity(core_client, entry.entry_id, {})

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "ignored" in error["message"]


@pytest.mark.usefixtures("flow_domain")
@pytest.mark.parametrize("source", ["ignore", "user"])
async def test_options_flow_is_offered_only_for_an_entry_that_is_not_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient, source: str
) -> None:
    entry = _add_entry(hass, source)

    if source == "ignore":
        with pytest.raises(ToolError) as exc_info:
            await options_target(core_client, entry.entry_id)
        assert "ignored" in json.loads(str(exc_info.value))["error"]["message"]
    else:
        target = await options_target(core_client, entry.entry_id)
        assert (target.kind, target.domain, target.entry_id) == (
            "options",
            _DOMAIN,
            entry.entry_id,
        )
    assert not hass.config_entries.options.async_progress()


@pytest.mark.usefixtures("flow_domain")
@pytest.mark.parametrize("source", ["ignore", "user"])
async def test_options_restore_writes_only_an_entry_that_is_not_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient, source: str
) -> None:
    entry = _add_entry(hass, source)

    if source == "ignore":
        with pytest.raises(OptionsFlowError) as exc_info:
            await restore_config_entry_options(
                core_client, entry.entry_id, {"host": "h"}
            )
        assert exc_info.value.apply_status == "not_applied"
        assert not hass.config_entries.options.async_progress()
        assert entry.options == {}
    else:
        await restore_config_entry_options(core_client, entry.entry_id, {"host": "h"})
        assert entry.options == {"host": "h"}
