"""``enabled`` and ``run_actions`` without a config write, on real Home Assistant Core.

``ha_config_set_automation(entity_id=..., enabled=...)`` turns an automation
on or off and ``run_actions=True`` runs its actions, without touching the
stored config. Each test runs the tool on the server's own client against a
``hass`` that runs ``automation.morning``.
"""

from __future__ import annotations

import functools
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import Settings
from ha_mcp.tools import tools_config_automations, ws_waiters

from ._automation_enabled_core import (
    CONFIG_ID,
    ENTITY_ID,
    RAN,
    config_hash,
    morning,
    morning_tools,
    set_automation,
    snapshots,
    state_of,
    turn_off_in_core,
)
from ._backup_core import backup_settings as backup_settings
from ._config_dir_yaml import write_config_files
from ._receipt_faults import lose_state_reads, on_receipt
from ._tool_errors import error_of
from ._tools_config_automations_core import require_vendored_skills

# A blueprint automation, for the take-control write.
_BLUEPRINT = """\
blueprint:
  name: Ping
  domain: automation
  input:
    event_type: {}
triggers:
  - trigger: event
    event_type: !input event_type
actions:
  - event: pinged
"""
_PING = {
    "id": "ping-id",
    "alias": "Ping",
    "use_blueprint": {"path": "local/ping.yaml", "input": {"event_type": "ping"}},
}


async def test_enabled_by_config_id_turns_the_automation_off(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config id names no entity: the tool turns off the automation Core
    runs under it, leaves its config alone, and reports its entity_id."""
    tools = await morning_tools(hass, core_client, monkeypatch)

    result = await set_automation(tools, entity_id=CONFIG_ID, enabled=False, wait=False)

    assert result["action"] == "set_enabled"
    assert result["automation_id"] == ENTITY_ID
    assert result["enabled_applied"] is True
    assert state_of(hass) == "off"
    stored = await core_client.get_automation_config(CONFIG_ID)
    assert stored == {"id": CONFIG_ID, **morning()}


async def test_enabled_attaches_the_skill_files_by_default(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The automation skill files ship with every set call unless the caller
    opts out, a runtime-only one too."""
    require_vendored_skills()
    tools = await morning_tools(hass, core_client, monkeypatch)

    result = await tools.ha_config_set_automation(
        entity_id=ENTITY_ID, enabled=True, wait=False
    )

    assert result["action"] == "set_enabled"
    assert result["skill_content"]


@pytest.mark.parametrize(
    "runtime", [{"enabled": False}, {"run_actions": True}], ids=["enabled", "run"]
)
async def test_runtime_change_that_gets_no_answer_fails_the_call(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    runtime: dict[str, Any],
) -> None:
    """With no config write, the runtime change is the whole call: a lost
    service call is a connection failure, not a partial success."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    monkeypatch.setattr(
        core_client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError("service unavailable")),
    )

    with pytest.raises(ToolError) as exc_info:
        await set_automation(tools, entity_id=ENTITY_ID, wait=False, **runtime)

    error = error_of(exc_info)
    assert error["code"] == "CONNECTION_FAILED"
    assert "service unavailable" in error["message"]


async def test_lost_state_read_is_a_connection_failure_not_a_missing_automation(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config id is resolved from Core's states: when that read gets no
    answer, the caller is told the connection failed, not that the
    automation does not exist."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    monkeypatch.setattr(
        core_client,
        "get_states",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )

    with pytest.raises(ToolError) as exc_info:
        await set_automation(tools, entity_id=CONFIG_ID, enabled=False, wait=False)

    assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
    assert state_of(hass) == "on"


@pytest.mark.parametrize("state_lost", [False, True], ids=["reached", "lost"])
async def test_enabled_with_wait_reports_whether_the_state_was_reached(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    state_lost: bool,
) -> None:
    """``wait=True`` confirms the automation reports ``on``; when its state is
    gone as the wait starts, the caller is told it could not be verified."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    await turn_off_in_core(hass)
    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_state_change",
        functools.partial(ws_waiters.wait_for_state_change, timeout=0.1),
    )

    with on_receipt(
        hass,
        "subscribe_events",
        lambda msg: hass.states.async_remove(ENTITY_ID),
        when=lambda msg: state_lost and msg.get("event_type") == "state_changed",
    ):
        result = await set_automation(tools, entity_id=ENTITY_ID, enabled=True)

    assert result["enabled_applied"] is True
    if state_lost:
        assert any("could not be verified" in w for w in result["warnings"]), result
    else:
        assert "warnings" not in result, result
        assert state_of(hass) == "on"


async def test_enabled_whose_verification_gets_no_answer_still_reports_applied(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core turned the automation on; only the check after it was lost, so
    the change is reported applied with a warning, not as a failure."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    await turn_off_in_core(hass)
    lose_state_reads(core_client, monkeypatch)

    result = await set_automation(tools, entity_id=ENTITY_ID, enabled=True)

    assert result["enabled_applied"] is True
    assert any("verification failed" in w for w in result["warnings"]), result
    assert state_of(hass) == "on"


@pytest.mark.parametrize(
    ("call", "refusal"),
    [
        ({"entity_id": CONFIG_ID, "category": "morning"}, "category requires a config"),
        ({}, "entity_id is required"),
        ({"entity_id": "automation.missing"}, "Automation not found"),
    ],
    ids=["category", "no-entity-id", "missing"],
)
async def test_standalone_enabled_is_refused_without_a_usable_target(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    call: dict[str, str],
    refusal: str,
) -> None:
    """A category needs a config write, and an absent or unknown automation
    cannot be turned off: each is refused and nothing is turned off."""
    tools = await morning_tools(hass, core_client, monkeypatch)

    with pytest.raises(ToolError) as exc_info:
        await set_automation(tools, enabled=False, wait=False, **call)

    assert refusal in error_of(exc_info)["message"]
    assert state_of(hass) == "on"


@pytest.mark.parametrize(
    ("call", "snapshotted"),
    [
        ({"entity_id": ENTITY_ID, "enabled": False}, False),
        ({"entity_id": ENTITY_ID, "run_actions": True}, False),
        ({"entity_id": ENTITY_ID, "enabled": False, "config": morning(2)}, True),
        (
            {
                "entity_id": ENTITY_ID,
                "run_actions": True,
                "python_transform": "config['description'] = 'x'",
            },
            True,
        ),
        (
            {
                "entity_id": "automation.ping",
                "enabled": False,
                "take_control_of_blueprint": True,
            },
            True,
        ),
    ],
    ids=["enabled", "run", "config", "python_transform", "take_control"],
)
async def test_auto_backup_skips_only_calls_that_write_no_config(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    backup_settings: Settings,
    call: dict[str, Any],
    snapshotted: bool,
) -> None:
    """Turning an automation on or off or running it changes no config, so it
    takes no snapshot; a write that also does either still takes one."""
    await write_config_files(
        hass, {"blueprints/automation/local/ping.yaml": _BLUEPRINT}
    )
    tools = await morning_tools(hass, core_client, monkeypatch, _PING)
    if "python_transform" in call:
        call = {**call, "config_hash": await config_hash(tools, ENTITY_ID)}

    await set_automation(tools, wait=False, **call)

    assert bool(snapshots(backup_settings)) is snapshotted


async def test_run_actions_by_config_id_runs_them_and_leaves_the_state(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_actions`` alone runs the automation once and changes nothing else."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    runs = async_capture_events(hass, RAN)

    result = await set_automation(
        tools, entity_id=CONFIG_ID, run_actions=True, wait=False
    )
    await hass.async_block_till_done()

    assert result["action"] == "run_actions"
    assert result["automation_id"] == ENTITY_ID
    assert result["actions_triggered"] is True
    assert "enabled" not in result
    assert len(runs) == 1
    assert state_of(hass) == "on"


async def test_enabled_and_run_actions_turn_on_before_running(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_actions`` is applied after ``enabled``, as the tool documents."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    await turn_off_in_core(hass)
    order: list[str] = []

    @callback
    def on_change(event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old and new and old.state != new.state:
            order.append(new.state)

    @callback
    def on_run(event: Event) -> None:
        order.append("ran")

    unsubscribe = async_track_state_change_event(hass, ENTITY_ID, on_change)
    stop_listening = hass.bus.async_listen(RAN, on_run)
    try:
        result = await set_automation(
            tools, entity_id=ENTITY_ID, enabled=True, run_actions=True, wait=False
        )
        await hass.async_block_till_done()
    finally:
        unsubscribe()
        stop_listening()

    assert result["action"] == "set_enabled"
    assert result["enabled_applied"] is True
    assert result["actions_triggered"] is True
    assert order == ["on", "ran"]


async def test_run_lost_after_enabled_landed_is_a_partial_success(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The automation is already on when the run gets no answer: the call
    succeeds, keeps the state change, and says the run did not happen."""
    tools = await morning_tools(hass, core_client, monkeypatch)
    await turn_off_in_core(hass)

    @callback
    def lose_service_calls(event: Event[EventStateChangedData]) -> None:
        monkeypatch.setattr(
            core_client,
            "call_service",
            AsyncMock(side_effect=HomeAssistantConnectionError("trigger failed")),
        )

    unsubscribe = async_track_state_change_event(hass, ENTITY_ID, lose_service_calls)
    try:
        result = await set_automation(
            tools, entity_id=ENTITY_ID, enabled=True, run_actions=True, wait=False
        )
    finally:
        unsubscribe()

    assert result["enabled_applied"] is True
    assert result["actions_triggered"] is False
    assert any("actions could not be run" in w for w in result["warnings"]), result
    assert state_of(hass) == "on"
