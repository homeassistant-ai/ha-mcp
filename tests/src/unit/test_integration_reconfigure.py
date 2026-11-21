"""Reconfiguring a config entry through ``ha_set_integration(reconfigure=True)``.

The entry's reconfigure flow is a test integration's own, run by Core's flow
manager; its identity comes from Core's registries, and the reload after the
change is the one Core runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import config_entry_flow_progress, config_entry_reload_watch
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._integration_reconfigure_core import (
    HUB,
    MAC,
    NEW_HOST,
    OTHER,
    PLAIN,
    SECOND_FORM_HOST,
    UNIQUE_ID,
    Hub,
    HubWorld,
    add_hub,
    before_answering,
    config_flows,
    share_device,
    spies,
)
from ._integration_reconfigure_core import hub as hub
from ._integration_reconfigure_core import tools as tools
from ._integration_reconfigure_core import world as world
from ._tool_errors import rejected_call_body

APPLY = {"host": NEW_HOST, "reload": True}


def anchored(entry: MockConfigEntry, **anchors: Any) -> dict[str, Any]:
    """The arguments of a reconfigure call on ``entry``."""
    return {"entry_id": entry.entry_id, "reconfigure": True, **anchors}


# --- what a finished reconfigure reports ------------------------------------


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("loaded", "applied_and_verified"),
        ("setup_retry", "applied_but_unverified"),
        (None, "applied_but_unverified"),
    ],
    ids=["loaded", "setup-retry", "no-reload"],
)
@pytest.mark.parametrize("call", ["submit", "re-read"])
async def test_a_finished_reconfigure_reports_the_state_after_the_reload(
    hass: HomeAssistant,
    tools: IntegrationTools,
    hub: Hub,
    world: HubWorld,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | None,
    status: str,
    call: str,
) -> None:
    """Verified only when the reload Core runs after the change loads the
    entry; a re-read that finishes a flow whose progress step ended between
    calls reports the same."""
    if outcome is None:
        monkeypatch.setattr(config_entry_reload_watch, "RELOAD_SETTLE_TIMEOUT", 0.3)
    if outcome == "setup_retry":
        world.setup_error = ConfigEntryNotReady("host unreachable")
    target = anchored(hub.entry, expected_mac=MAC)
    answer = {"host": NEW_HOST, "reload": outcome is not None}

    if call == "submit":
        result = await tools.ha_set_integration(config=answer, **target)
    else:
        world.applied.clear()
        monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
        waiting = await rejected_call_body(
            tools.ha_set_integration(config=answer, **target)
        )
        assert waiting["error"]["code"] == "TIMEOUT_OPERATION"
        world.applied.set()
        await hass.async_block_till_done()
        result = await tools.ha_set_integration(flow_id=waiting["flow_id"], **target)

    assert result["status"] == status
    assert result["entry_state"] == outcome
    assert hub.entry.data["host"] == NEW_HOST


async def test_a_reconfigure_answered_with_another_form_reports_no_state(
    tools: IntegrationTools, hub: Hub
) -> None:
    """Only a finished flow waits for the reload: a second form applied nothing."""
    result = await tools.ha_set_integration(
        config={"host": SECOND_FORM_HOST, "reload": True},
        **anchored(hub.entry, expected_mac=MAC),
    )

    assert result["step_id"] == "confirm"
    assert "status" not in result
    assert "entry_state" not in result
    assert hub.entry.data["host"] == "old"


# --- the entry's identity ----------------------------------------------------


async def test_an_offline_entry_is_reconfigured_from_its_registry_identity(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    tools: IntegrationTools,
    hub: Hub,
    world: HubWorld,
) -> None:
    """An entry Core cannot set up is changed without reaching the device;
    another integration's entry on the same device is reported, not refused."""
    world.setup_error = ConfigEntryNotReady("device offline")
    await hass.config_entries.async_reload(hub.entry.entry_id)
    assert hub.entry.state is ConfigEntryState.SETUP_RETRY
    other = share_device(hass, device_registry, OTHER)

    result = await tools.ha_set_integration(
        config=APPLY,
        **anchored(
            hub.entry,
            expected_device_id=hub.device_id,
            expected_mac="AA-BB-CC-00-11-22",
            expected_entity_ids=[hub.entity_id],
        ),
    )

    assert result["success"] is True
    assert result["reason"] == "reconfigure_successful"
    assert (result["entry_id"], result["domain"]) == (hub.entry.entry_id, HUB)
    (warning,) = result["warnings"]
    assert f"{OTHER} ({other.entry_id})" in warning
    assert hub.entry.data["host"] == NEW_HOST


async def test_a_second_entry_of_the_integration_on_the_device_blocks_the_reconfigure(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    """A same-domain twin is a duplicate-integration risk, even when the
    caller's anchors match."""
    share_device(hass, device_registry, HUB)

    body = await rejected_call_body(
        tools.ha_set_integration(
            config=APPLY, **anchored(hub.entry, expected_device_id=hub.device_id)
        )
    )

    assert "same domain" in body["error"]["message"]
    assert config_flows(hass) == []
    assert hub.entry.data["host"] == "old"


async def test_a_helper_entry_on_the_device_does_not_block_or_warn(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    """Core attaches helpers such as switch_as_x to their source device."""
    helper = share_device(hass, device_registry, "switch_as_x")
    entity_registry.async_get_or_create(
        "light", "switch_as_x", "derived", config_entry=helper, device_id=hub.device_id
    )

    result = await tools.ha_set_integration(
        config=APPLY, **anchored(hub.entry, expected_device_id=hub.device_id)
    )

    assert result["success"] is True
    assert "warnings" not in result
    assert hub.entry.data["host"] == NEW_HOST


async def test_an_entry_on_the_device_that_is_gone_by_the_check_blocks_it(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    component_core_client: HomeAssistantClient,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    """The device still listed a helper entry that was removed before the
    entries were read: its domain cannot be told, so the check fails closed."""
    helper = share_device(hass, device_registry, "switch_as_x")

    async def remove_helper() -> None:
        await hass.config_entries.async_remove(helper.entry_id)

    with before_answering(hass, "ha_mcp_tools/config_entries", remove_helper):
        body = await rejected_call_body(
            tools.ha_set_integration(
                config=APPLY, **anchored(hub.entry, expected_device_id=hub.device_id)
            )
        )

    assert "same domain" in body["error"]["message"]
    assert helper.entry_id in body["before_identity"]["related_entry_ids"]
    assert config_flows(hass) == []
    assert hub.entry.data["host"] == "old"


@pytest.mark.parametrize(
    ("anchor", "message"),
    [
        ({"expected_mac": "11:22:33:44:55:66"}, "expected MAC"),
        ({"expected_device_id": "device-kitchen"}, "expected device_id"),
        ({"expected_entity_ids": ["switch.kitchen"]}, "expected entity_ids"),
    ],
    ids=["mac", "device_id", "entity_ids"],
)
async def test_an_anchor_the_registries_contradict_stops_the_reconfigure(
    hass: HomeAssistant,
    tools: IntegrationTools,
    hub: Hub,
    anchor: dict[str, Any],
    message: str,
) -> None:
    body = await rejected_call_body(
        tools.ha_set_integration(config=APPLY, **anchored(hub.entry, **anchor))
    )

    assert message in body["error"]["message"]
    assert config_flows(hass) == []
    assert hub.entry.data["host"] == "old"


@pytest.mark.parametrize(
    ("connection", "expected_mac", "matches"),
    [
        ({"connections": {(dr.CONNECTION_NETWORK_MAC, MAC)}}, MAC, True),
        ({"identifiers": {(HUB, "AA:BB:CC:DD:EE:FF")}}, "aabbccddeeff", True),
        (
            {"connections": {(dr.CONNECTION_UPNP, "uuid:0000-1111-2222-3333")}},
            "uuid:0000-1111-2222-3333",
            False,
        ),
    ],
    ids=["mac-connection", "mac-shaped-identifier", "upnp-uuid"],
)
async def test_expected_mac_matches_hardware_addresses_only(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    tools: IntegrationTools,
    connection: dict[str, Any],
    expected_mac: str,
    matches: bool,
) -> None:
    """A MAC connection or a MAC-shaped identifier anchors expected_mac; a
    UPnP UUID is not a hardware address."""
    entry = add_hub(hass)
    device_registry.async_get_or_create(config_entry_id=entry.entry_id, **connection)
    call = tools.ha_set_integration(
        config=APPLY, **anchored(entry, expected_mac=expected_mac)
    )

    if matches:
        assert (await call)["success"] is True
    else:
        body = await rejected_call_body(call)
        assert "expected MAC" in body["error"]["message"]
        assert config_flows(hass) == []


async def test_a_device_without_entities_anchors_the_entry(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    tools: IntegrationTools,
) -> None:
    entry = add_hub(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    result = await tools.ha_set_integration(
        config=APPLY, **anchored(entry, expected_device_id=device.id, expected_mac=MAC)
    )

    assert result["success"] is True
    assert entry.data["host"] == NEW_HOST


@pytest.mark.parametrize(
    ("unique_id", "matches"),
    [(UNIQUE_ID, True), ("another-serial", False)],
    ids=["matches", "differs"],
)
async def test_expected_unique_id_is_checked_with_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tools: IntegrationTools,
    hub: Hub,
    unique_id: str,
    matches: bool,
) -> None:
    """Only the ha_mcp_tools component can read an entry's unique_id."""
    call = tools.ha_set_integration(
        config=APPLY, **anchored(hub.entry, expected_unique_id=unique_id)
    )

    if matches:
        assert (await call)["success"] is True
    else:
        body = await rejected_call_body(call)
        assert "expected unique_id" in body["error"]["message"]
        assert config_flows(hass) == []


async def test_expected_unique_id_is_refused_without_the_component(
    hass: HomeAssistant, tools: IntegrationTools, hub: Hub
) -> None:
    """The refusal names the real reason and the anchors that work instead."""
    body = await rejected_call_body(
        tools.ha_set_integration(
            config=APPLY, **anchored(hub.entry, expected_unique_id=UNIQUE_ID)
        )
    )

    assert "ha_mcp_tools" in body["error"]["message"]
    assert "expected_device_id" in body["error"]["message"]
    assert config_flows(hass) == []


async def test_an_entry_with_no_device_or_entity_is_refused_without_the_component(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """Nothing but its unique_id could anchor such an entry, and only the
    component can read that."""
    entry = add_hub(hass)

    body = await rejected_call_body(
        tools.ha_set_integration(config=APPLY, **anchored(entry))
    )

    assert "identity anchor" in body["error"]["message"]
    assert config_flows(hass) == []
    assert entry.data["host"] == "old"


async def test_with_the_component_the_unique_id_anchors_an_entry_with_no_device(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tools: IntegrationTools,
) -> None:
    entry = add_hub(hass)

    result = await tools.ha_set_integration(config=APPLY, **anchored(entry))

    assert result["success"] is True
    assert entry.data["host"] == NEW_HOST


# --- the flow ------------------------------------------------------------------


async def test_the_client_opens_cores_reconfigure_flow_for_the_entry(
    hass: HomeAssistant, core_client: HomeAssistantClient, hub: Hub
) -> None:
    """Naming the entry makes Core open the flow with the reconfigure source."""
    started = await core_client.start_reconfigure_flow(HUB, hub.entry.entry_id)

    (flow,) = config_flows(hass)
    assert flow["flow_id"] == started["flow_id"]
    assert flow["context"]["source"] == "reconfigure"
    assert flow["context"]["entry_id"] == hub.entry.entry_id
    assert started["step_id"] == "reconfigure"


async def test_an_entry_whose_integration_cannot_reconfigure_is_refused(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, tools: IntegrationTools
) -> None:
    entry = MockConfigEntry(domain=PLAIN)
    entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    body = await rejected_call_body(
        tools.ha_set_integration(config=APPLY, **anchored(entry, expected_mac=MAC))
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "reconfigure" in body["error"]["message"]
    assert config_flows(hass) == []


async def test_a_flow_core_aborts_as_it_opens_submits_nothing(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
) -> None:
    entry = add_hub(hass, opens="abort")
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    with spies(core_client, "submit_config_flow_step") as calls:
        body = await rejected_call_body(
            tools.ha_set_integration(config=APPLY, **anchored(entry, expected_mac=MAC))
        )

    assert "cannot_connect" in body["error"]["message"]
    calls["submit_config_flow_step"].assert_not_called()
    assert entry.data["host"] == "old"


async def test_a_browser_step_is_refused_its_flow_aborted_and_nothing_applied(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    tools: IntegrationTools,
) -> None:
    entry = add_hub(hass, opens="external")
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    body = await rejected_call_body(
        tools.ha_set_integration(config=APPLY, **anchored(entry, expected_mac=MAC))
    )

    assert body["status"] == "apply_failed"
    assert "browser" in body["error"]["message"]
    assert config_flows(hass) == []


async def test_a_registry_that_cannot_be_read_stops_the_reconfigure(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    """The identity checks need the registries: no answer is no reconfigure."""
    with patch.object(
        core_client,
        "list_entity_registry",
        AsyncMock(side_effect=HomeAssistantConnectionError("registry unavailable")),
    ):
        body = await rejected_call_body(
            tools.ha_set_integration(
                config=APPLY, **anchored(hub.entry, expected_mac=MAC)
            )
        )

    assert body["success"] is False
    assert "registry unavailable" in body["error"]["message"]
    assert config_flows(hass) == []
    assert hub.entry.data["host"] == "old"


async def test_home_assistant_unreachable_is_a_structured_error_for_the_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    with patch.object(
        core_client,
        "get_config_entry",
        AsyncMock(side_effect=HomeAssistantConnectionError("HA unreachable")),
    ):
        body = await rejected_call_body(
            tools.ha_set_integration(
                config=APPLY, **anchored(hub.entry, expected_mac=MAC)
            )
        )

    assert body["success"] is False
    assert body["error"]["code"]
    assert body["entry_id"] == hub.entry.entry_id
    assert config_flows(hass) == []


async def test_each_reconfigure_step_checks_the_identity_again(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: IntegrationTools,
    hub: Hub,
) -> None:
    """Every call that may submit re-reads the entry's identity first; an abort
    does not, so a flow whose entry cannot be read can still be ended. A
    finished flow is forgotten, so it is neither continued nor ended."""
    target = anchored(hub.entry, expected_mac=MAC)
    methods = (
        "get_config_entry",
        "start_reconfigure_flow",
        "submit_config_flow_step",
        "abort_config_flow",
    )

    with spies(core_client, *methods) as calls:
        opened = await tools.ha_set_integration(**target)
        done = await tools.ha_set_integration(
            flow_id=opened["flow_id"], config=APPLY, **target
        )
        await rejected_call_body(
            tools.ha_set_integration(flow_id=opened["flow_id"], abort=True, **target)
        )

    assert opened["step_id"] == "reconfigure"
    assert done["message"] == f"{HUB} integration reconfigured"
    assert calls["get_config_entry"].await_count == 2
    calls["start_reconfigure_flow"].assert_awaited_once()
    calls["submit_config_flow_step"].assert_awaited_once_with(opened["flow_id"], APPLY)
    calls["abort_config_flow"].assert_not_called()

    reopened = await tools.ha_set_integration(**target)
    with (
        spies(core_client, "abort_config_flow") as calls,
        patch.object(
            core_client,
            "get_config_entry",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection dropped")),
        ),
    ):
        aborted = await tools.ha_set_integration(
            flow_id=reopened["flow_id"], abort=True, **target
        )

    assert aborted["aborted"] is True
    calls["abort_config_flow"].assert_awaited_once_with(reopened["flow_id"])
    assert config_flows(hass) == []
