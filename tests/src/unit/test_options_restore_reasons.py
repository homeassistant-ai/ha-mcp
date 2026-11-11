"""Restore diagnostics identify refusals without echoing snapshot or HA values."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import (
    OptionsFlowError,
    restore_config_entry_options,
)

DOMAIN = "restore_probe"

# What the probe's options flow received from Home Assistant's submit route.
submissions: list[dict[str, Any]] = []


class _ProbeOptionsFlow(OptionsFlow):
    """Another integration's options flow, answering as its entry's scenario says."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.config_entry.data["scenario"]
        if scenario == "unsupported_form":
            return self.async_show_menu(
                step_id="init", menu_options=["secret-marker-menu"]
            )
        if user_input is not None:
            submissions.append(user_input)
            if scenario == "abort":
                return self.async_abort(reason="secret-marker-abort")
        errors = None
        if scenario == "initial_validation" or (
            scenario == "submit_validation" and user_input is not None
        ):
            errors = {
                "state": "secret-marker-error",
                "secret_marker_key": "secret-marker-value",
            }
        elif user_input is not None:
            return self.async_create_entry(data=user_input)
        # A number field, so a string value is a malformed submission for Core.
        state = int if scenario == "rejected" else str
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("state"): state}),
            errors=errors,
            last_step=True,
        )


class _ProbeConfigFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _ProbeOptionsFlow()


@pytest.fixture
async def probe_integration(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[None]:
    submissions.clear()
    mock_integration(hass, MockModule(DOMAIN))
    mock_platform(hass, f"{DOMAIN}.config_flow", None)
    with mock_config_flow(DOMAIN, _ProbeConfigFlow):
        assert await async_setup_component(hass, "config", {})
        yield


async def _refused_restore(
    client: HomeAssistantClient, entry_id: str, config: dict[str, Any]
) -> OptionsFlowError:
    with pytest.raises(OptionsFlowError) as caught:
        await restore_config_entry_options(
            client, entry_id, config, expected_domain=DOMAIN
        )
    return caught.value


# ``reached_flow``: the flow's own step received the submission. A rejected one
# stops in Core's schema validation, and a lost connection never gets there.
@pytest.mark.parametrize(
    ("scenario", "reason", "fields", "reached_flow"),
    [
        ("unsupported_fields", "unsupported_fields", ("unit_of_measurement",), False),
        ("unsupported_form", "unsupported_form", (), False),
        ("initial_validation", "validation_failed", ("state",), False),
        ("submit_validation", "validation_failed", ("state",), True),
        ("abort", "flow_aborted", (), True),
        ("rejected", "validation_failed", (), False),
        ("connection", None, (), False),
    ],
)
async def test_restore_reason_never_echoes_values_or_unknown_keys(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    probe_integration: None,
    scenario: str,
    reason: str | None,
    fields: tuple[str, ...],
    reached_flow: bool,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"scenario": scenario})
    entry.add_to_hass(hass)
    config: dict[str, Any] = {"state": "secret-marker-template"}
    if scenario == "unsupported_fields":
        config.update(
            unit_of_measurement="secret-marker-unit",
            secret_marker_key="secret-marker-value",
        )

    if scenario == "connection":
        with patch.object(
            core_client,
            "submit_options_flow_step",
            AsyncMock(
                side_effect=HomeAssistantConnectionError("secret-marker-transport")
            ),
        ):
            error = await _refused_restore(core_client, entry.entry_id, config)
    else:
        error = await _refused_restore(core_client, entry.entry_id, config)

    assert error.reason == reason
    assert error.fields == fields
    assert "secret-marker" not in str(error)
    assert "secret_marker_key" not in str(error)
    assert error.apply_status == (
        "unknown" if scenario == "connection" else "not_applied"
    )
    assert len(submissions) == int(reached_flow)
    open_flows = hass.config_entries.options.async_progress()
    if scenario == "connection":
        # No completion reply, so the flow is left open, not aborted.
        assert [flow["flow_id"] for flow in open_flows] == [error.flow_id]
    else:
        assert open_flows == []
