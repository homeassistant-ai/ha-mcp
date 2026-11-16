"""The ``wait`` parameter of the config and service tools (#381), on real Core.

``wait`` (default True) makes a write confirm its entity reached Core before
the tool returns; ``wait=False`` returns at once. Each tool runs on the
server's own client connected to a real ``hass``; a spy on the tool's waiter
calls through, so every wait still reads Core.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import (
    tools_config_automations,
    tools_config_scripts,
    tools_service,
    ws_waiters,
)
from ha_mcp.tools.config_helpers import create as helper_create
from ha_mcp.tools.config_helpers import update as helper_update
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_service import ServiceTools

from ._automation_core import entity_id_of, load_automations_file
from ._config_writes import automation_tools, script_tools
from ._helper_tool_core import create_item, fresh_name, set_up
from ._receipt_faults import at_entity_wait, lose_state_reads, on_receipt

AUTOMATION = {
    "alias": "Test Auto",
    # An event trigger: a time trigger leaves Core's timer behind the test.
    "trigger": [{"platform": "event", "event_type": "test_event"}],
    "action": [{"service": "input_boolean.turn_on"}],
}
SCRIPT = {"alias": "Test", "sequence": [{"delay": {"seconds": 1}}]}

WAITS = pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])


@contextmanager
def spy(module: object, name: str) -> Iterator[AsyncMock]:
    """Spy on ``module``'s waiter ``name``; the real wait still runs."""
    with patch.object(module, name, wraps=getattr(module, name)) as waiter:
        yield waiter


def removal_waits() -> AbstractContextManager[AsyncMock]:
    """Spy on the removal wait every config remove tool goes through."""
    return spy(ws_waiters, "wait_for_entity_removed")


def assert_waited(waiter: AsyncMock, wait: bool, *args: Any) -> None:
    if wait:
        waiter.assert_awaited_once_with(*args)
    else:
        waiter.assert_not_awaited()


# --- automations ------------------------------------------------------------


@WAITS
async def test_set_automation_confirms_its_entity_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    wait: bool,
) -> None:
    """Without the wait, a caller acting on the new automation at once may
    find nothing; with ``wait=False`` the tool must not spend the wait."""
    tools = await automation_tools(hass, core_client, monkeypatch)

    with spy(tools_config_automations, "wait_for_entity_registered") as waiter:
        result = await tools.ha_config_set_automation(config=AUTOMATION, wait=wait)

    assert result["success"] is True
    assert "warnings" not in result, result
    assert_waited(waiter, wait, core_client, "automation.test_auto")


async def test_set_automation_whose_entity_never_appears_warns_not_yet_queryable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write landed, so it is a success, but the caller is told the entity
    is not there yet. The entity's state is gone when the wait starts."""
    tools = await automation_tools(hass, core_client, monkeypatch)
    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_entity_registered",
        functools.partial(ws_waiters.wait_for_entity_registered, timeout=0.1),
    )

    with at_entity_wait(hass, lambda: hass.states.async_remove("automation.test_auto")):
        result = await tools.ha_config_set_automation(config=AUTOMATION)

    assert result["success"] is True
    assert any("not yet queryable" in w for w in result["warnings"]), result


@WAITS
async def test_remove_automation_confirms_removal_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    wait: bool,
) -> None:
    tools = await automation_tools(hass, core_client, monkeypatch)
    await tools.ha_config_set_automation(config=AUTOMATION, wait=False)

    with removal_waits() as waiter:
        result = await tools.ha_config_remove_automation(
            entity_id="automation.test_auto", wait=wait
        )

    assert result["success"] is True
    assert "warnings" not in result, result
    assert hass.states.get("automation.test_auto") is None
    assert_waited(waiter, wait, core_client, "automation.test_auto")


async def _stored_automation(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> tools_config_automations.AutomationConfigTools:
    """The automation tools, with Core holding the automation of config id 12345."""
    tools = await automation_tools(hass, client, monkeypatch)
    await load_automations_file(
        hass, "automations.yaml", [{"id": "12345", **AUTOMATION}]
    )
    return tools


async def test_remove_automation_by_config_id_waits_for_its_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config id is no entity_id: the removal waits for the entity Core
    gave the automation, not for the id the caller typed."""
    tools = await _stored_automation(hass, core_client, monkeypatch)
    entity_id = entity_id_of(hass, "12345")

    with removal_waits() as waiter:
        result = await tools.ha_config_remove_automation(entity_id="12345")

    assert result["success"] is True
    assert result["automation_id"] == entity_id
    waiter.assert_awaited_once_with(core_client, entity_id)


async def test_remove_automation_by_config_id_without_state_answer_still_deletes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no answer to the states read, the entity is unknown: the delete
    still goes through and nothing is waited for."""
    tools = await _stored_automation(hass, core_client, monkeypatch)
    entity_id = entity_id_of(hass, "12345")

    with (
        patch.object(
            core_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection error")),
        ),
        removal_waits() as waiter,
    ):
        result = await tools.ha_config_remove_automation(entity_id="12345")

    assert result["success"] is True
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    waiter.assert_not_awaited()


# --- scripts ----------------------------------------------------------------


@WAITS
async def test_set_script_confirms_its_entity_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    wait: bool,
) -> None:
    tools = await script_tools(hass, core_client, monkeypatch)

    with spy(tools_config_scripts, "wait_for_entity_registered") as waiter:
        result = await tools.ha_config_set_script(
            script_id="test_script", config=SCRIPT, wait=wait
        )

    assert result["success"] is True
    assert "warnings" not in result, result
    assert_waited(waiter, wait, core_client, "script.test_script")


@WAITS
async def test_remove_script_confirms_removal_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    wait: bool,
) -> None:
    tools = await script_tools(hass, core_client, monkeypatch)
    await tools.ha_config_set_script(script_id="test_script", config=SCRIPT, wait=False)

    with removal_waits() as waiter:
        result = await tools.ha_config_remove_script(script_id="test_script", wait=wait)

    assert result["success"] is True
    assert "warnings" not in result, result
    assert_waited(waiter, wait, core_client, "script.test_script")


# --- helpers ----------------------------------------------------------------


@WAITS
async def test_create_helper_confirms_its_entity_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    wait: bool,
) -> None:
    await set_up(hass, "input_boolean")

    with spy(helper_create, "wait_for_entity_registered") as waiter:
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", name=fresh_name(), wait=wait
        )

    assert (result["success"], result["action"]) == (True, "create")
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", result["data"]["id"]
    )
    assert_waited(waiter, wait, core_client, entity_id)


@WAITS
async def test_update_helper_confirms_its_entity_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    wait: bool,
) -> None:
    await set_up(hass, "input_boolean")
    item_id = await create_item(core_client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )

    with spy(helper_update, "wait_for_entity_registered") as waiter:
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean",
            helper_id=entity_id,
            name=fresh_name(),
            wait=wait,
        )

    assert (result["success"], result["action"]) == (True, "update")
    assert_waited(waiter, wait, core_client, entity_id)


# --- ha_call_service --------------------------------------------------------


@pytest.fixture
async def service_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> ServiceTools:
    """The service tool without the component: Core's REST call, then the wait.
    ``input_boolean.lamp`` is off."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"lamp": {"initial": False}}}
    )
    return ServiceTools(core_client, DeviceControlTools(core_client))


@WAITS
async def test_call_service_verifies_the_new_state_only_when_asked(
    hass: HomeAssistant, service_tools: ServiceTools, wait: bool
) -> None:
    with spy(tools_service, "wait_for_state_change") as waiter:
        result = await service_tools.ha_call_service(
            domain="input_boolean",
            service="turn_on",
            entity_id="input_boolean.lamp",
            wait=wait,
        )

    assert result["success"] is True
    assert result.get("verified_state") == ("on" if wait else None)
    assert waiter.await_count == (1 if wait else 0)
    lamp = hass.states.get("input_boolean.lamp")
    assert lamp is not None and lamp.state == "on"


@pytest.mark.parametrize(
    ("domain", "service", "target"),
    [
        # A press moves the button to a timestamp no wait could expect.
        ("input_button", "press", {"entity_id": "input_button.bell"}),
        # A scene's own state never becomes "on" (#1515).
        ("scene", "turn_on", {"entity_id": "scene.movie"}),
        # No entity_id argument names the entity to watch.
        ("input_boolean", "turn_on", {"data": {"entity_id": "input_boolean.lamp"}}),
    ],
    ids=["service-with-no-end-state", "scene", "no-entity-argument"],
)
async def test_call_service_does_not_wait_for_a_change_it_cannot_see(
    hass: HomeAssistant,
    service_tools: ServiceTools,
    domain: str,
    service: str,
    target: dict[str, Any],
) -> None:
    """Waiting here would always run out the full timeout and end in a
    spurious "could not be verified" warning."""
    assert await async_setup_component(
        hass, "input_button", {"input_button": {"bell": {}}}
    )
    assert await async_setup_component(
        hass,
        "scene",
        {"scene": [{"name": "Movie", "entities": {"input_boolean.lamp": "on"}}]},
    )
    # Core adds the platform's entities after the setup call returns.
    await hass.async_block_till_done()

    with spy(tools_service, "wait_for_state_change") as waiter:
        result = await service_tools.ha_call_service(
            domain=domain, service=service, **target
        )

    assert result["success"] is True
    assert "verified_state" not in result
    assert "warnings" not in result, result
    waiter.assert_not_awaited()


async def test_call_service_toggle_is_verified_by_any_change(
    service_tools: ServiceTools,
) -> None:
    """A toggle has no known end state; the change itself is the proof."""
    result = await service_tools.ha_call_service(
        domain="input_boolean", service="toggle", entity_id="input_boolean.lamp"
    )

    assert result["success"] is True
    assert result["verified_state"] == "on"


async def test_call_service_without_answer_to_its_wait_still_succeeds(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    service_tools: ServiceTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call went out and landed; losing the check after it must not turn
    it into a failure the caller would retry. State reads lose their answer
    when the wait subscribes."""
    with on_receipt(
        hass,
        "subscribe_events",
        lambda msg: lose_state_reads(core_client, monkeypatch),
        when=lambda msg: msg.get("event_type") == "state_changed",
    ):
        result = await service_tools.ha_call_service(
            domain="input_boolean", service="turn_on", entity_id="input_boolean.lamp"
        )

    assert result["success"] is True
    assert any("state verification failed" in w for w in result["warnings"]), result
    lamp = hass.states.get("input_boolean.lamp")
    assert lamp is not None and lamp.state == "on"
