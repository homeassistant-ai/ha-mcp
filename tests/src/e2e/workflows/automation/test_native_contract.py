"""E2E: the automation config tools hand the config to Core as written and
report Core's answer (#2632).

Each test names a case where the tools once answered differently from Core:
refusing what Core accepts, rewriting what Core refuses, misreporting a
create, or failing to find an automation Core still stores.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_condition, wait_for_tool_result

pytestmark = pytest.mark.automation


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _config(tag: str, **extra: Any) -> dict[str, Any]:
    return {
        "alias": f"E2E native contract {tag}",
        "triggers": [{"trigger": "event", "event_type": f"e2e_native_{tag}"}],
        "actions": [{"delay": {"seconds": 1}}],
        **extra,
    }


async def _remove(mcp_client: Any, entity_id: str) -> None:
    await safe_call_tool(
        mcp_client, "ha_config_remove_automation", {"entity_id": entity_id}
    )


async def test_automation_without_alias_is_created(mcp_client) -> None:
    """Core accepts an automation with no alias and names it itself, so the
    tool must not refuse it for a missing alias."""
    config = _config(_tag())
    del config["alias"]
    entity_id = None
    async with MCPAssertions(mcp_client) as mcp:
        try:
            created = await mcp.call_tool_success(
                "ha_config_set_automation", {"config": config}
            )
            entity_id = created["entity_id"]
            stored = await mcp.call_tool_success(
                "ha_config_get_automation", {"entity_id": entity_id}
            )
            assert "alias" not in stored["config"], stored
            assert stored["config"]["triggers"] == config["triggers"], stored
        finally:
            if entity_id:
                await _remove(mcp_client, entity_id)


async def test_both_root_forms_are_refused_with_cores_text(mcp_client) -> None:
    """A config holding both ``trigger`` and ``triggers`` is refused with
    Core's own message, and nothing is written: the tool no longer keeps one
    form and saves the automation under a warning."""
    tag = _tag()
    config_id = f"e2e_both_roots_{tag}"
    config = _config(tag)
    config["trigger"] = list(config["triggers"])
    async with MCPAssertions(mcp_client) as mcp:
        try:
            rejected = await mcp.call_tool_failure(
                "ha_config_set_automation",
                {"entity_id": config_id, "config": config},
                expected_error=(
                    "Cannot specify both 'trigger' and 'triggers'. "
                    "Please use 'triggers' only."
                ),
            )
            assert rejected["error"]["code"] == "VALIDATION_FAILED", rejected
            missing = await mcp.call_tool_failure(
                "ha_config_get_automation", {"entity_id": config_id}
            )
            assert missing["error"]["code"] == "RESOURCE_NOT_FOUND", missing
        finally:
            await _remove(mcp_client, config_id)


async def test_fresh_explicit_id_reports_created_then_updated(mcp_client) -> None:
    """Writing to a config id Core does not hold yet is a create; writing to
    it again is an update. The tool once reported the first write as an
    update (#2903)."""
    tag = _tag()
    config_id = f"e2e_created_{tag}"
    config = _config(tag)
    async with MCPAssertions(mcp_client) as mcp:
        try:
            first = await mcp.call_tool_success(
                "ha_config_set_automation", {"entity_id": config_id, "config": config}
            )
            assert first["operation"] == "created", first
            second = await mcp.call_tool_success(
                "ha_config_set_automation", {"entity_id": config_id, "config": config}
            )
            assert second["operation"] == "updated", second
        finally:
            await _remove(mcp_client, config_id)


async def test_removing_an_unstored_config_id_is_not_found(mcp_client) -> None:
    """Core answers the DELETE of a config id it does not store with a 400,
    not a 404; the tool still reports the automation as not found and lists
    the ids that do exist, instead of a validation failure."""
    async with MCPAssertions(mcp_client) as mcp:
        missing = await mcp.call_tool_failure(
            "ha_config_remove_automation",
            {"entity_id": f"e2e_never_stored_{_tag()}"},
        )
    assert missing["error"]["code"] == "RESOURCE_NOT_FOUND", missing
    assert isinstance(missing["available_automation_ids"], list), missing


async def test_registry_disabled_automation_is_removed_by_entity_id(
    mcp_client, ha_client
) -> None:
    """A registry-disabled automation has no state, so only the entity
    registry still maps its entity_id to the config; removing it by entity_id
    must find it there."""
    tag = _tag()
    config_id = f"e2e_disabled_{tag}"

    async def _state_gone() -> bool:
        states = await ha_client.get_states()
        return all(state.get("entity_id") != entity_id for state in states)

    async with MCPAssertions(mcp_client) as mcp:
        try:
            created = await mcp.call_tool_success(
                "ha_config_set_automation",
                {"entity_id": config_id, "config": _config(tag)},
            )
            entity_id = created["entity_id"]
            # ha_set_entity refuses to registry-disable an automation, so the
            # test disables it through Core's registry command directly.
            reply = await ha_client.send_websocket_message(
                {
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                    "disabled_by": "user",
                }
            )
            assert reply.get("success") is True, reply
            assert await wait_for_condition(
                _state_gone, timeout=20, condition_name=f"{entity_id} state removed"
            )

            await mcp.call_tool_success(
                "ha_config_remove_automation", {"entity_id": entity_id}
            )
            gone = await mcp.call_tool_failure(
                "ha_config_get_automation", {"entity_id": config_id}
            )
            assert gone["error"]["code"] == "RESOURCE_NOT_FOUND", gone
        finally:
            await _remove(mcp_client, config_id)


async def test_get_reports_registry_entry_and_no_load_failure(mcp_client) -> None:
    """The read carries the automation's registry entry, so labels set
    through ha_set_entity are visible next to the config, and reports no
    validation failure for an automation Core loaded."""
    tag = _tag()
    config_id = f"e2e_registry_{tag}"
    label_id = None
    async with MCPAssertions(mcp_client) as mcp:
        try:
            label = await mcp.call_tool_success(
                "ha_config_set_label", {"name": f"E2E automation label {tag}"}
            )
            label_id = label["label_id"]
            created = await mcp.call_tool_success(
                "ha_config_set_automation",
                {"entity_id": config_id, "config": _config(tag)},
            )
            entity_id = created["entity_id"]
            await mcp.call_tool_success(
                "ha_set_entity", {"entity_id": entity_id, "labels": [label_id]}
            )

            got = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_get_automation",
                arguments={"entity_id": entity_id},
                predicate=lambda d: (
                    label_id in ((d.get("entity") or {}).get("labels") or [])
                ),
                description="automation read carries the new label",
            )
            assert got["entity"]["entity_id"] == entity_id, got
            assert got["validation"] is None, got
        finally:
            await _remove(mcp_client, config_id)
            if label_id:
                await safe_call_tool(
                    mcp_client, "ha_config_remove_label", {"label_id": label_id}
                )


@pytest.mark.requires_component_surface
async def test_unknown_area_target_is_warned(mcp_client) -> None:
    """Core saves a service call aimed at an area that does not exist; the
    reference validator, fed the component's registry ids, warns about it."""
    tag = _tag()
    config_id = f"e2e_area_target_{tag}"
    area_id = f"e2e_no_such_area_{tag}"
    config = _config(
        tag,
        actions=[{"action": "light.turn_on", "target": {"area_id": area_id}}],
    )
    async with MCPAssertions(mcp_client) as mcp:
        try:
            created = await mcp.call_tool_success(
                "ha_config_set_automation", {"entity_id": config_id, "config": config}
            )
            warnings = (created.get("validation") or {}).get("warnings") or []
            assert any(w.get("value") == area_id for w in warnings), created
        finally:
            await _remove(mcp_client, config_id)


@pytest.mark.requires_component_surface
async def test_component_validates_with_cores_own_validator(ha_client) -> None:
    """The automation_config bridge finds Core's validator on this Core: a
    missing one would answer ``unavailable`` and every write would fall back
    to Core's REST answer without the bridge."""
    reply = await ha_client.send_websocket_message(
        {
            "type": "ha_mcp_tools/automation_config",
            "action": "validate",
            "config": {
                "triggers": [{"trigger": "battery.low"}],
                "actions": [{"delay": {"seconds": 1}}],
            },
        }
    )
    assert reply.get("success") is True, reply
    assert reply["result"]["status"] == "failed_triggers", reply
    assert "Invalid trigger 'battery.low' specified" in reply["result"]["error"], reply
