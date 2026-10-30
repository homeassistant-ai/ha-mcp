"""E2E: a todo item backup puts an edited item back in place and adds a removed
one again with its status (#2632).

The list is a local_todo list, which supports descriptions and due dates;
like the local_calendar lane it runs on HAOS only.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import _enable_auto_backup, _wait_for_backup


@pytest.fixture
async def todo_list(mcp_client, ha_client) -> AsyncIterator[str]:
    suffix = uuid.uuid4().hex[:8]
    entity_id = f"todo.e2e_bk_todo_{suffix}"
    flow = await ha_client.start_config_flow("local_todo")
    done = await ha_client.submit_config_flow_step(
        flow["flow_id"], {"todo_list_name": f"E2E BK Todo {suffix}"}
    )
    assert done.get("type") == "create_entry", done
    try:
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_entity",
            arguments={"entity_id": entity_id},
            predicate=lambda d: d.get("success") is True,
            description=f"{entity_id} registers",
            timeout=20,
        )
        yield entity_id
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": done["result"]["entry_id"], "confirm": True},
        )


async def _wait_for_item(
    mcp_client, entity_id: str, what: str, **expected: Any
) -> dict[str, Any]:
    """Poll the list until an item carries every expected field (exactly the
    keys given; ``None`` means the item has no such field)."""

    def match(data: dict[str, Any]) -> bool:
        return any(
            all(item.get(k) == v for k, v in expected.items())
            for item in data.get("items", [])
        )

    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_todo",
        arguments={"entity_id": entity_id},
        predicate=match,
        description=what,
        timeout=20,
    )
    return next(
        item
        for item in data["items"]
        if all(item.get(k) == v for k, v in expected.items())
    )


async def _restore(mcp: MCPAssertions, backup_name: str) -> None:
    await mcp.call_tool_success(
        "ha_manage_backup",
        {"scope": "edits", "action": "restore", "backup_name": backup_name},
    )


@pytest.mark.haos_only
@pytest.mark.external_only
@pytest.mark.cleanup
class TestTodoCaptureRestore:
    async def test_edited_item_gets_its_fields_back_under_its_uid(
        self, mcp_client, todo_list: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        summary = f"bk-item-{uuid.uuid4().hex[:8]}"
        async with MCPAssertions(mcp_client) as mcp:
            added = await mcp.call_tool_success(
                "ha_set_todo_item",
                {
                    "entity_id": todo_list,
                    "summary": summary,
                    "description": "keep me",
                    "due_date": "2030-11-01",
                },
            )
            uid = added["todo_item"]["uid"]

            await mcp.call_tool_success(
                "ha_set_todo_item",
                {
                    "entity_id": todo_list,
                    "item": summary,
                    "rename": f"{summary}-edited",
                    "status": "completed",
                    "description": "",
                    "due_datetime": "2030-12-24T10:00:00",
                },
            )
            await _wait_for_item(
                mcp_client,
                todo_list,
                "edit landed",
                uid=uid,
                summary=f"{summary}-edited",
                status="completed",
                description=None,
            )

            backup = await _wait_for_backup(
                mcp_client, domain="todo_item", entity_id=f"{todo_list}::{uid}"
            )
            await _restore(mcp, backup)

            await _wait_for_item(
                mcp_client,
                todo_list,
                "item back to its pre-edit fields",
                uid=uid,
                summary=summary,
                status="needs_action",
                description="keep me",
                due="2030-11-01",
            )

    async def test_removed_item_is_added_again_with_its_status(
        self, mcp_client, todo_list: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        summary = f"bk-item-{uuid.uuid4().hex[:8]}"
        async with MCPAssertions(mcp_client) as mcp:
            added = await mcp.call_tool_success(
                "ha_set_todo_item",
                {"entity_id": todo_list, "summary": summary, "description": "d"},
            )
            uid = added["todo_item"]["uid"]
            # Both this edit and the removal back the item up under its uid;
            # the list gives the removal's, the newest, first.
            await mcp.call_tool_success(
                "ha_set_todo_item",
                {"entity_id": todo_list, "item": uid, "status": "completed"},
            )
            await mcp.call_tool_success(
                "ha_remove_todo_item", {"entity_id": todo_list, "item": summary}
            )

            backup = await _wait_for_backup(
                mcp_client, domain="todo_item", entity_id=f"{todo_list}::{uid}"
            )
            await _restore(mcp, backup)

            item = await _wait_for_item(
                mcp_client,
                todo_list,
                "removed item added again",
                summary=summary,
                status="completed",
                description="d",
            )
            assert item["uid"] != uid
