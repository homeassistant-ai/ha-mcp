"""ha_set_integration's modes on real Home Assistant Core (issue #1814).

Enable/disable, add (config flow), update options (options flow) and
reconfigure are mutually exclusive. The tool runs on the server's own client
against a real ``hass``; the flows are a test integration's own, run by Core's
flow managers, and every refusal below comes before Core is asked anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import answer_translations, open_flow_ids
from ._tool_errors import rejected_call_body

DOMAIN = "routed_flow"
MAC = "AA:BB:CC:DD:EE:FF"


class _Options(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema({vol.Optional("scan_interval"): int})
        )


class _Flow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required("name"): str})
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema({vol.Required("host"): str})
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _Options()


@pytest.fixture
def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> Iterator[IntegrationTools]:
    """The tool on an integration with a setup, an options and a reconfigure flow."""
    mock_integration(
        hass,
        MockModule(
            DOMAIN,
            async_setup_entry=AsyncMock(return_value=None),
            async_unload_entry=AsyncMock(return_value=None),
        ),
    )
    mock_platform(hass, f"{DOMAIN}.config_flow")
    answer_translations(hass)
    with mock_config_flow(DOMAIN, _Flow):
        yield IntegrationTools(core_client)


@pytest.fixture
async def entry(hass: HomeAssistant, tools: IntegrationTools) -> MockConfigEntry:
    assert await async_setup_component(hass, "config", {})
    added = MockConfigEntry(domain=DOMAIN, title="Hub")
    added.add_to_hass(hass)
    return added


@pytest.fixture
def writes(core_client: HomeAssistantClient) -> Iterator[dict[str, Mock]]:
    """Call-through spies on what would start a change in Core."""
    with (
        patch.object(
            core_client, "start_config_flow", wraps=core_client.start_config_flow
        ) as config_flow,
        patch.object(
            core_client, "start_options_flow", wraps=core_client.start_options_flow
        ) as options_flow,
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as websocket,
    ):
        yield {"config": config_flow, "options": options_flow, "ws": websocket}


async def _refused(call: Any, fragment: str) -> dict[str, Any]:
    body = await rejected_call_body(call)
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert fragment in body["error"]["message"], body["error"]["message"]
    return body


def _assert_nothing_started(hass: HomeAssistant, writes: dict[str, Mock]) -> None:
    for spy in writes.values():
        spy.assert_not_called()
    assert open_flow_ids(hass) == []


# --- arguments that name two modes, or none ----------------------------------------


async def test_entry_id_and_domain_together_are_refused(
    hass: HomeAssistant,
    tools: IntegrationTools,
    entry: MockConfigEntry,
    writes: dict[str, Mock],
) -> None:
    await _refused(
        tools.ha_set_integration(entry_id=entry.entry_id, domain=DOMAIN), "not both"
    )
    _assert_nothing_started(hass, writes)


@pytest.mark.parametrize(
    "call",
    [{"domain": DOMAIN}, {"config": {"scan_interval": 30}}],
    ids=["with_domain", "with_config"],
)
async def test_enabled_beside_another_mode_is_refused(
    hass: HomeAssistant,
    tools: IntegrationTools,
    entry: MockConfigEntry,
    writes: dict[str, Mock],
    call: dict[str, Any],
) -> None:
    entry_id = None if "domain" in call else entry.entry_id

    await _refused(
        tools.ha_set_integration(entry_id=entry_id, enabled=True, **call),
        "mutually exclusive",
    )
    _assert_nothing_started(hass, writes)
    assert entry.disabled_by is None


async def test_a_call_naming_no_mode_is_refused(tools: IntegrationTools) -> None:
    await _refused(tools.ha_set_integration(), "Nothing to do")


async def test_identity_guards_are_refused_outside_reconfigure(
    hass: HomeAssistant,
    tools: IntegrationTools,
    entry: MockConfigEntry,
    writes: dict[str, Mock],
) -> None:
    """The identity guards must not ride an unguarded options update."""
    body = await _refused(
        tools.ha_set_integration(
            entry_id=entry.entry_id,
            config={"scan_interval": 30},
            expected_device_id="device-1",
        ),
        "reconfigure",
    )

    assert body["rejected_parameters"] == ["expected_device_id"]
    _assert_nothing_started(hass, writes)
    assert dict(entry.options) == {}


async def test_set_integration_has_explicit_safety_annotations(
    tools: IntegrationTools,
) -> None:
    """Clients ask before a destructive, non-idempotent call."""
    annotations = tools.ha_set_integration.__fastmcp__.annotations
    assert annotations.open_world_hint is False
    assert annotations.destructive_hint is True
    assert annotations.read_only_hint is False
    assert annotations.idempotent_hint is False


# --- each mode reaches its own flow ------------------------------------------------


async def test_a_domain_with_config_adds_the_integration(
    hass: HomeAssistant, tools: IntegrationTools, entry: MockConfigEntry
) -> None:
    result = await tools.ha_set_integration(domain=DOMAIN, config={"name": "Office"})

    assert result["success"] is True
    titles = [e.title for e in hass.config_entries.async_entries(DOMAIN)]
    assert sorted(titles) == ["Hub", "Office"]


async def test_an_entry_id_alone_opens_its_options_form(
    hass: HomeAssistant, tools: IntegrationTools, entry: MockConfigEntry
) -> None:
    result = await tools.ha_set_integration(entry_id=entry.entry_id)

    assert [f["flow_id"] for f in hass.config_entries.options.async_progress()] == [
        result["flow_id"]
    ]
    assert dict(entry.options) == {}


async def test_an_entry_id_with_config_saves_its_options(
    tools: IntegrationTools, entry: MockConfigEntry
) -> None:
    await tools.ha_set_integration(
        entry_id=entry.entry_id, config={"scan_interval": 30}
    )

    assert dict(entry.options) == {"scan_interval": 30}


@pytest.mark.parametrize("reconfigure", [False, True], ids=["options", "reconfigure"])
async def test_abort_ends_the_flow_the_tool_opened(
    hass: HomeAssistant,
    tools: IntegrationTools,
    identified_entry: MockConfigEntry,
    reconfigure: bool,
) -> None:
    opened = await tools.ha_set_integration(
        entry_id=identified_entry.entry_id, reconfigure=reconfigure
    )
    assert open_flow_ids(hass) == [opened["flow_id"]]

    await tools.ha_set_integration(
        entry_id=identified_entry.entry_id,
        reconfigure=reconfigure,
        flow_id=opened["flow_id"],
        abort=True,
    )

    assert open_flow_ids(hass) == []


@pytest.fixture
def identified_entry(
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> MockConfigEntry:
    """The entry with a device that reports ``MAC`` and one entity on it."""
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )
    entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        "relay",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="relay",
    )
    return entry


async def test_reconfigure_opens_the_reconfigure_form_when_the_identity_holds(
    hass: HomeAssistant,
    tools: IntegrationTools,
    identified_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    device = dr.async_entries_for_config_entry(
        device_registry, identified_entry.entry_id
    )[0]

    result = await tools.ha_set_integration(
        entry_id=identified_entry.entry_id,
        reconfigure=True,
        expected_device_id=device.id,
        expected_mac=MAC,
        expected_entity_ids=["switch.relay"],
    )

    flows = hass.config_entries.flow.async_progress()
    assert [(f["flow_id"], f["context"]["source"]) for f in flows] == [
        (result["flow_id"], SOURCE_RECONFIGURE)
    ]
    assert hass.config_entries.options.async_progress() == []


async def test_reconfigure_of_an_entry_without_that_identity_opens_no_flow(
    hass: HomeAssistant, tools: IntegrationTools, identified_entry: MockConfigEntry
) -> None:
    body = await rejected_call_body(
        tools.ha_set_integration(
            entry_id=identified_entry.entry_id,
            reconfigure=True,
            expected_device_id="another-device",
        )
    )

    assert "device_id" in body["error"]["message"]
    assert open_flow_ids(hass) == []


# --- enable / disable ------------------------------------------------------------


async def test_disabling_an_entry_marks_it_disabled_by_the_user(
    hass: HomeAssistant, tools: IntegrationTools, entry: MockConfigEntry
) -> None:
    """Core unloads the loaded entry, so no restart is needed."""
    assert await hass.config_entries.async_setup(entry.entry_id)

    result = await tools.ha_set_integration(entry_id=entry.entry_id, enabled=False)

    assert result["success"] is True
    assert result["entry_id"] == entry.entry_id
    assert result["require_restart"] is False
    assert entry.disabled_by is ConfigEntryDisabler.USER


async def test_enabling_an_entry_clears_its_disabled_by(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    assert await async_setup_component(hass, "config", {})
    disabled = MockConfigEntry(domain=DOMAIN, disabled_by=ConfigEntryDisabler.USER)
    disabled.add_to_hass(hass)

    result = await tools.ha_set_integration(entry_id=disabled.entry_id, enabled=True)

    assert result["success"] is True
    assert disabled.disabled_by is None
