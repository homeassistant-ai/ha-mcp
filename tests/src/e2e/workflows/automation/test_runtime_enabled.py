"""Runtime control through ha_config_set_automation(enabled=..., run_actions=...)."""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions
from ...utilities.wait_helpers import wait_for_tool_result


def _config(identifier: str, **extra: object) -> dict[str, object]:
    return {
        "alias": f"Runtime enabled {identifier}",
        "triggers": [{"trigger": "event", "event_type": identifier}],
        "actions": [{"stop": "Throwaway"}],
        **extra,
    }


async def _state(mcp: MCPAssertions, entity_id: str) -> str:
    result = await mcp.call_tool_success(
        "ha_get_state", {"entity_id": entity_id, "fields": ["state"]}
    )
    return str(result["data"]["state"])


@pytest.mark.automation
async def test_create_disabled_then_enable_standalone(mcp_client, cleanup_tracker):
    """Create with enabled=False, then toggle by unique_id without a config."""
    mcp = MCPAssertions(mcp_client)
    identifier = f"runtime_enabled_{uuid4().hex}"
    cleanup_tracker.track("automation", identifier)

    created = await mcp.call_tool_success(
        "ha_config_set_automation",
        {"identifier": identifier, "config": _config(identifier), "enabled": False},
    )
    entity_id = created["automation_id"]
    assert entity_id.startswith("automation.")
    assert created["enabled_applied"] is True
    assert await _state(mcp, entity_id) == "off"

    stored = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )
    assert "enabled" not in stored["config"]

    enabled = await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": identifier, "enabled": True}
    )
    assert enabled["action"] == "set_enabled"
    assert await _state(mcp, entity_id) == "on"

    await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": entity_id, "enabled": False}
    )
    assert await _state(mcp, entity_id) == "off"


@pytest.mark.automation
async def test_config_write_disable_survives_reload(mcp_client, cleanup_tracker):
    """enabled=False on a changed config must outlast the reload HA runs for it.

    initial_state: true makes the rebuilt entity come up on, so a turn_off
    sent before the reload replaced the entity would be undone.
    """
    mcp = MCPAssertions(mcp_client)
    identifier = f"runtime_enabled_{uuid4().hex}"
    cleanup_tracker.track("automation", identifier)
    config = _config(identifier, initial_state=True)

    created = await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": identifier, "config": config}
    )
    entity_id = created["automation_id"]
    assert await _state(mcp, entity_id) == "on"

    updated = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": identifier,
            "config": {**config, "description": "changed"},
            "enabled": False,
            "wait": False,
        },
    )
    assert updated["enabled_applied"] is True
    assert not any("automation reload" in w for w in updated.get("warnings", []))
    assert await _state(mcp, entity_id) == "off"


@pytest.mark.automation
async def test_python_transform_cannot_store_enabled(mcp_client, cleanup_tracker):
    """A transform that injects 'enabled' is rejected and nothing is written."""
    mcp = MCPAssertions(mcp_client)
    identifier = f"runtime_enabled_{uuid4().hex}"
    cleanup_tracker.track("automation", identifier)

    await mcp.call_tool_success(
        "ha_config_set_automation",
        {"identifier": identifier, "config": _config(identifier)},
    )
    before = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )

    await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": identifier,
            "python_transform": "config['enabled'] = False",
            "config_hash": before["config_hash"],
        },
        expected_error="runtime-only",
    )

    after = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": identifier}
    )
    assert after["config_hash"] == before["config_hash"]


async def _last_triggered_after(mcp_client, entity_id: str, previous: object) -> object:
    """Wait for the automation's last_triggered attribute to move past ``previous``."""
    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_state",
        arguments={"entity_id": entity_id, "fields": ["attributes"]},
        predicate=lambda d: (
            d.get("data", {}).get("attributes", {}).get("last_triggered")
            not in (None, previous)
        ),
        description=f"{entity_id} last_triggered moves past {previous!r}",
    )
    return data["data"]["attributes"]["last_triggered"]


@pytest.mark.automation
async def test_run_actions_standalone_and_after_write(mcp_client, cleanup_tracker):
    """run_actions runs the automation now, alone or after a config write."""
    mcp = MCPAssertions(mcp_client)
    identifier = f"runtime_enabled_{uuid4().hex}"
    cleanup_tracker.track("automation", identifier)
    config = _config(identifier)

    created = await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": identifier, "config": config}
    )
    entity_id = created["automation_id"]

    ran = await mcp.call_tool_success(
        "ha_config_set_automation", {"identifier": identifier, "run_actions": True}
    )
    assert ran["action"] == "run_actions"
    assert ran["actions_triggered"] is True
    first = await _last_triggered_after(mcp_client, entity_id, None)

    updated = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": identifier,
            "config": {**config, "description": "changed"},
            "run_actions": True,
        },
    )
    assert updated["actions_triggered"] is True
    assert not any("automation reload" in w for w in updated.get("warnings", []))
    await _last_triggered_after(mcp_client, entity_id, first)
