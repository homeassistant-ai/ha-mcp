"""Native Template recovery through generic edit and deletion routes."""

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import (
    wait_for_condition,
    wait_for_entity_registration,
    wait_for_entity_state,
    wait_for_tool_result,
)


async def _create_template(mcp, template_type, name, state):
    return await mcp.call_tool_success(
        "ha_config_set_helper",
        {
            "helper_type": "template",
            "name": name,
            "config": {"next_step_id": template_type, "state": state},
        },
    )


async def _options(mcp_client, entry_id):
    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_config_list_helpers",
        arguments={"helper_type": "template"},
        predicate=lambda result: any(
            row.get("entry_id") == entry_id for row in result.get("helpers", [])
        ),
        description="Template options are available",
    )
    return next(
        row["options"] for row in data["helpers"] if row["entry_id"] == entry_id
    )


async def _backup_names(mcp, entry_id):
    result = await mcp.call_tool_success(
        "ha_manage_backup",
        {
            "scope": "edits",
            "action": "list",
            "domain": "helper_template",
            "entity_id": entry_id,
        },
    )
    return {row["name"] for row in result["data"]["backups"]}


async def _rename(mcp_client, entity_id, renamed, name):
    await MCPAssertions(mcp_client).call_tool_success(
        "ha_set_entity",
        {"entity_id": entity_id, "new_entity_id": renamed, "name": name},
    )
    assert await wait_for_entity_registration(mcp_client, renamed)


async def _assert_collision_refused(
    mcp_client, ha_client, entry_ids, saved, target, kind
):
    mcp = MCPAssertions(mcp_client)
    blocker = await _create_template(
        mcp,
        kind,
        f"Recovery occupant {uuid.uuid4().hex[:8]}",
        "{{ 77 }}" if kind == "sensor" else "{{ false }}",
    )
    blocker_id = blocker["entry_id"]
    entry_ids.add(blocker_id)
    await _rename(mcp_client, blocker["entity_ids"][0], target, "Occupied target")
    before = await mcp.call_tool_success("ha_get_entity", {"entity_id": target})
    helpers_before = await mcp.call_tool_success(
        "ha_config_list_helpers", {"helper_type": "template"}
    )
    refused = await mcp.call_tool_failure(
        "ha_manage_backup",
        {"scope": "edits", "action": "restore", "backup_name": saved},
    )
    assert "occupied" in refused["error"]["message"].lower(), refused
    after = await mcp.call_tool_success("ha_get_entity", {"entity_id": target})
    assert after["entity_entry"] == before["entity_entry"]
    helpers_after = await mcp.call_tool_success(
        "ha_config_list_helpers", {"helper_type": "template"}
    )
    assert {row["entry_id"] for row in helpers_after["helpers"]} == {
        row["entry_id"] for row in helpers_before["helpers"]
    }, "A collision must be refused before creating a replacement"
    await ha_client.delete_config_entry(blocker_id)
    entry_ids.remove(blocker_id)

    async def target_is_free():
        registry = await ha_client.list_entity_registry()
        states = await ha_client.get_states()
        return all(row["entity_id"] != target for row in [*registry, *states])

    assert await wait_for_condition(
        target_is_free,
        condition_name=f"{target} absent from both registry and states",
    ), "The deleted collision occupant must be gone before retrying restore"


@pytest.mark.helper
@pytest.mark.cleanup
class TestTemplateDeletedHelperRecovery:
    @pytest.mark.parametrize("template_type", ["sensor", "binary_sensor"])
    @pytest.mark.parametrize("collision", [False, True], ids=["renamed", "occupied"])
    @pytest.mark.parametrize("delete_route", ["entry", "alias"])
    async def test_generic_edit_delete_and_recreate(
        self, mcp_client, ha_client, template_type, collision, delete_route
    ):
        mcp = MCPAssertions(mcp_client)
        if not component_surface_available():
            failure = await mcp.call_tool_failure(
                "ha_config_list_helpers", {"helper_type": "template"}
            )
            assert failure["error"]["code"] == "COMPONENT_NOT_INSTALLED"
            return

        entry_ids = set()
        backup_targets = set()
        try:
            created = await _create_template(
                mcp,
                template_type,
                f"Recovery original {uuid.uuid4().hex[:8]}",
                "{{ 12 }}" if template_type == "sensor" else "{{ true }}",
            )
            original_id = created["entry_id"]
            entry_ids.add(original_id)
            backup_targets.add(original_id)
            target = f"{template_type}.restored_reference_{uuid.uuid4().hex[:8]}"
            label = f"Renamed recovery {uuid.uuid4().hex[:8]}"
            await _rename(mcp_client, created["entity_ids"][0], target, label)
            original_options = await _options(mcp_client, original_id)

            before_edit = await _backup_names(mcp, original_id)
            await mcp.call_tool_success(
                "ha_set_integration",
                {
                    "entry_id": original_id,
                    "config": {
                        "state": "{{ 99 }}"
                        if template_type == "sensor"
                        else "{{ false }}"
                    },
                },
            )
            assert await wait_for_entity_state(
                mcp_client, target, "99" if template_type == "sensor" else "off"
            )
            edit_backups = await _backup_names(mcp, original_id) - before_edit
            assert len(edit_backups) == 1, "Generic edits must capture Template options"
            (edit_backup,) = edit_backups
            await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": edit_backup},
            )
            assert await _options(mcp_client, original_id) == original_options

            before_delete = await _backup_names(mcp, original_id)
            delete_arguments = {"target": original_id, "confirm": True}
            if delete_route == "alias":
                delete_arguments.update(target=target, helper_type="template")
            await mcp.call_tool_success(
                "ha_remove_helpers_integrations",
                delete_arguments,
            )
            entry_ids.remove(original_id)
            delete_backups = await _backup_names(mcp, original_id) - before_delete
            assert len(delete_backups) == 1, (
                f"{delete_route} deletion must capture its own fresh snapshot"
            )
            (saved,) = delete_backups
            source = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": saved},
            )
            assert source["data"]["config"]["options"] == original_options
            assert source["data"]["config"]["entities"][0]["entity_id"] == target
            assert source["data"]["config"]["entities"][0]["name"] == label

            if collision:
                await _assert_collision_refused(
                    mcp_client, ha_client, entry_ids, saved, target, template_type
                )
            restored = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": saved},
            )
            outcome = restored["data"]
            replacement = outcome["result"]["entry_id"]
            entry_ids.add(replacement)
            backup_targets.add(replacement)
            assert replacement != original_id
            assert outcome["original_entry_id"] == original_id
            assert outcome["restore_mode"] == "recreated"
            assert await _options(mcp_client, replacement) == original_options
            assert await wait_for_entity_state(
                mcp_client, target, "12" if template_type == "sensor" else "on"
            ), "The recovered entity must function under its original renamed ID"
            entity = await mcp.call_tool_success("ha_get_entity", {"entity_id": target})
            assert entity["entity_entry"]["config_entry_id"] == replacement
            assert entity["entity_entry"]["name"] == label
            retained = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": saved},
            )
            assert retained["data"]["config"] == source["data"]["config"]
        finally:
            for entry_id in entry_ids:
                await ha_client.delete_config_entry(entry_id)
            for entry_id in backup_targets:
                await safe_call_tool(
                    mcp_client,
                    "ha_manage_backup",
                    {
                        "scope": "edits",
                        "action": "delete",
                        "domain": "helper_template",
                        "entity_id": entry_id,
                    },
                )
