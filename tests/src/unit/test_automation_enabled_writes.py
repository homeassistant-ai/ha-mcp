"""``enabled`` and ``run_actions`` on a config write, on real Home Assistant Core.

A write makes Core reload the automation, which replaces its entity, so the
tool applies the runtime change only once Core confirms the reload, to the
entity Core registered. Each test runs ``ha_config_set_automation`` on the
server's own client against Core's config API.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._automation_enabled_core import (
    CONFIG_ID,
    ENTITY_ID,
    RAN,
    config_hash,
    morning,
    morning_tools,
    registration_polls,
    set_automation,
    slow_reload,
    state_of,
    turn_off_in_core,
    unwatched_reload,
)
from ._command_faults import received
from ._config_writes import automation_tools
from ._tools_config_automations_core import stop_loading_automations, store_unloaded

MODES = pytest.mark.parametrize("mode", ["config", "python_transform"])


async def write_version_2(
    tools: AutomationConfigTools, mode: str, entity_id: str, **runtime: Any
) -> dict[str, Any]:
    """Change the automation's actions to version 2 through ``mode``."""
    if mode == "config":
        return await set_automation(
            tools, entity_id=entity_id, config=morning(version=2), **runtime
        )
    return await set_automation(
        tools,
        entity_id=entity_id,
        config_hash=await config_hash(tools, entity_id),
        python_transform="config['actions'][0]['event_data']['version'] = 2",
        **runtime,
    )


def lose_service_calls(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client's service calls get no answer from now on."""
    monkeypatch.setattr(
        client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError("service unavailable")),
    )


async def test_enabled_on_a_config_write_turns_the_automation_on_without_storing_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled`` is the automation's runtime state: it must survive the
    reload the write causes, and never become a key Core stores."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    await turn_off_in_core(hass)

    result = await write_version_2(tools, "config", ENTITY_ID, enabled=True)

    assert result["enabled"] is True
    assert result["enabled_applied"] is True
    assert "warnings" not in result, result
    await hass.async_block_till_done()
    assert state_of(hass) == "on"
    stored = await core_client.get_automation_config(CONFIG_ID)
    assert stored == {"id": CONFIG_ID, **morning(version=2)}


@MODES
async def test_enabled_on_a_write_by_config_id_reaches_the_reloaded_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A write by config id names no entity, and the reload replaces it: even
    with ``wait=False`` the tool finds the entity Core registered, turns it
    off and reports it."""
    tools = await morning_tools(hass, core_client, monkeypatch)

    result = await write_version_2(tools, mode, CONFIG_ID, enabled=False, wait=False)

    assert result["enabled_applied"] is True
    assert result["automation_id"] == ENTITY_ID
    assert result["entity_id"] == ENTITY_ID
    await hass.async_block_till_done()
    assert state_of(hass) == "off"


@pytest.mark.parametrize(
    "change",
    [
        {"config": morning(enabled=False)},
        {"python_transform": "config['enabled'] = False"},
    ],
    ids=["config", "python_transform"],
)
async def test_enabled_written_into_the_config_is_refused_before_writing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    """Core has no ``enabled`` config key: the caller is pointed at the
    ``enabled`` parameter and the stored automation is left as it was."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    if "python_transform" in change:
        change = {**change, "config_hash": await config_hash(tools, ENTITY_ID)}

    with pytest.raises(ToolError) as exc_info:
        await set_automation(tools, entity_id=ENTITY_ID, **change)

    assert "enabled" in str(exc_info.value)
    stored = await core_client.get_automation_config(CONFIG_ID)
    assert stored == {"id": CONFIG_ID, **morning()}


async def test_enabled_lost_after_a_config_write_is_a_partial_success(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write landed, so the tool succeeds, but says the state change did
    not: the caller must not retry the write."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    lose_service_calls(core_client, monkeypatch)

    result = await write_version_2(tools, "config", ENTITY_ID, enabled=False)

    assert result["success"] is True
    assert result["enabled_requested"] is False
    assert result["enabled_applied"] is False
    assert any("config was written" in w for w in result["warnings"]), result
    assert state_of(hass) == "on"
    stored = await core_client.get_automation_config(CONFIG_ID)
    assert stored == {"id": CONFIG_ID, **morning(version=2)}


@pytest.mark.parametrize(
    "target", [{"entity_id": "new-automation-id"}, {}], ids=["config-id", "generated"]
)
async def test_enabled_on_a_creation_reaches_the_new_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    target: dict[str, str],
) -> None:
    """A new automation's entity_id is Core's choice: the tool turns off the
    entity Core registered for it and reports that entity."""
    tools = await morning_tools(hass, core_client, monkeypatch)

    result = await set_automation(
        tools, config=morning(alias="Generated"), enabled=False, **target
    )

    assert result["automation_id"] == "automation.generated"
    assert result["enabled_applied"] is True
    assert state_of(hass, "automation.generated") == "off"


@pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
async def test_runtime_change_for_an_automation_core_never_loads_names_a_retry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    wait: bool,
) -> None:
    """configuration.yaml does not load automations.yaml, so the stored
    automation the write updates gets no entity. The tool polls for it once,
    applies nothing, and gives the call that applies every requested change
    later."""
    tools = await automation_tools(hass, core_client, monkeypatch)
    await store_unloaded(hass, [{"id": CONFIG_ID, **morning()}])
    await stop_loading_automations(hass)
    runs = async_capture_events(hass, RAN)

    with registration_polls() as polls:
        result = await write_version_2(
            tools, "config", CONFIG_ID, enabled=False, run_actions=True, wait=wait
        )

    polls.assert_awaited_once_with(core_client, CONFIG_ID)
    assert result["enabled_requested"] is False
    assert result["enabled_applied"] is False
    assert result["actions_triggered"] is False
    assert any(
        f"entity_id='{CONFIG_ID}', enabled=False, run_actions=True" in w
        for w in result["warnings"]
    ), result
    assert runs == []


@pytest.mark.parametrize("runtime", [{"enabled": False}, {}], ids=["enabled", "none"])
async def test_reload_is_watched_only_for_a_runtime_change_and_released(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    runtime: dict[str, Any],
) -> None:
    """Only a runtime change waits for the reload, and its subscription ends
    with the call: none piles up on Core's connection, write after write."""
    tools = await morning_tools(hass, core_client, monkeypatch)

    with received(hass, "subscribe_events", "unsubscribe_events") as seen:
        result = await write_version_2(tools, "config", ENTITY_ID, **runtime)

    watched = [
        msg["id"]
        for msg in seen["subscribe_events"]
        if msg.get("event_type") == "automation_reloaded"
    ]
    assert len(watched) == (1 if runtime else 0)
    released = [msg["subscription"] for msg in seen["unsubscribe_events"]]
    assert set(watched) <= set(released)
    assert "warnings" not in result, result


@pytest.mark.parametrize("unconfirmed", ["slow", "unwatched"])
async def test_enabled_after_an_unconfirmed_reload_warns_it_may_be_reverted(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    unconfirmed: str,
) -> None:
    """A reload still running when the state change lands can revert it, so
    the caller is told why it may not stick."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    expected = {
        "slow": "did not confirm the automation reload",
        "unwatched": "could not be watched for the automation reload",
    }[unconfirmed]
    reload = (
        slow_reload(hass, monkeypatch)
        if unconfirmed == "slow"
        else unwatched_reload(hass, core_client)
    )

    async with reload:
        result = await write_version_2(
            tools, "config", ENTITY_ID, enabled=False, wait=False
        )

    assert result["enabled_applied"] is True
    assert any(expected in w for w in result["warnings"]), result


@MODES
async def test_run_actions_on_a_write_runs_the_written_actions(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The run waits for Core to load the written automation, so it runs the
    new actions, not the ones the write replaced."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    runs = async_capture_events(hass, RAN)

    result = await write_version_2(tools, mode, ENTITY_ID, run_actions=True)
    await hass.async_block_till_done()

    assert result["actions_triggered"] is True
    assert "enabled_applied" not in result
    assert "warnings" not in result, result
    assert [run.data["version"] for run in runs] == [2]


@MODES
async def test_run_actions_on_a_write_is_skipped_when_the_reload_is_unconfirmed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A run cannot be undone, and before the reload it would run the old
    actions: the tool runs nothing and gives the call that runs them."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    runs = async_capture_events(hass, RAN)

    async with unwatched_reload(hass, core_client):
        result = await write_version_2(
            tools, mode, ENTITY_ID, run_actions=True, wait=False
        )
    await hass.async_block_till_done()

    assert result["actions_triggered"] is False
    assert any(
        "actions were not run" in w
        and f"entity_id='{ENTITY_ID}', run_actions=True" in w
        for w in result["warnings"]
    ), result
    assert runs == []


@MODES
async def test_run_actions_lost_after_a_write_is_a_partial_success(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The write landed, so the tool succeeds and says the run did not."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    hash_before = await config_hash(tools, ENTITY_ID)
    lose_service_calls(core_client, monkeypatch)

    result = await write_version_2(tools, mode, ENTITY_ID, run_actions=True)

    assert result["success"] is True
    assert result["actions_triggered"] is False
    assert any("actions could not be run" in w for w in result["warnings"]), result
    assert await config_hash(tools, ENTITY_ID) != hash_before
