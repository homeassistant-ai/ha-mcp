"""E2E: the todo tools hand their input to Core's todo services and report
Core's answer (#2632).

Runs on the base image's Shopping List (``todo.shopping_list``), which
supports creating, updating, deleting and moving items but neither
descriptions nor due dates.
"""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool

LIST = "todo.shopping_list"


def _names(result: dict) -> list[str]:
    return [item["summary"] for item in result["items"]]


@pytest.mark.todo
class TestTodoNativeContract:
    async def test_status_filter_takes_a_list(self, mcp_client) -> None:
        tag = uuid.uuid4().hex[:8]
        open_item, done_item = f"e2e-open-{tag}", f"e2e-done-{tag}"
        async with MCPAssertions(mcp_client) as mcp:
            try:
                for summary in (open_item, done_item):
                    await mcp.call_tool_success(
                        "ha_set_todo_item", {"entity_id": LIST, "summary": summary}
                    )
                await mcp.call_tool_success(
                    "ha_set_todo_item",
                    {"entity_id": LIST, "item": done_item, "status": "completed"},
                )

                both = await mcp.call_tool_success(
                    "ha_get_todo",
                    {"entity_id": LIST, "status": ["needs_action", "completed"]},
                )
                assert {open_item, done_item} <= set(_names(both))
                done = await mcp.call_tool_success(
                    "ha_get_todo", {"entity_id": LIST, "status": "completed"}
                )
                assert done_item in _names(done)
                assert open_item not in _names(done)
            finally:
                for summary in (open_item, done_item):
                    await safe_call_tool(
                        mcp_client,
                        "ha_remove_todo_item",
                        {"entity_id": LIST, "item": summary},
                    )

    async def test_unsupported_field_is_refused_by_core(self, mcp_client) -> None:
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_set_todo_item",
                {
                    "entity_id": LIST,
                    "summary": f"e2e-desc-{uuid.uuid4().hex[:8]}",
                    "description": "not on a shopping list",
                },
                expected_error="does not support setting field: description",
            )

    async def test_move_and_bulk_remove(self, mcp_client) -> None:
        tag = uuid.uuid4().hex[:8]
        first, second = f"e2e-first-{tag}", f"e2e-second-{tag}"
        async with MCPAssertions(mcp_client) as mcp:
            try:
                created = {}
                for summary in (first, second):
                    reply = await mcp.call_tool_success(
                        "ha_set_todo_item", {"entity_id": LIST, "summary": summary}
                    )
                    created[summary] = reply["todo_item"]["uid"]

                moved = await mcp.call_tool_success(
                    "ha_set_todo_item",
                    {"entity_id": LIST, "item": second, "move_after": ""},
                )
                assert moved["todo_item"]["uid"] == created[second]
                listed = await mcp.call_tool_success("ha_get_todo", {"entity_id": LIST})
                assert _names(listed)[0] == second

                # An update with a move: through the component's bridge where it
                # serves todo_item, through todo/item/move and todo.update_item
                # otherwise.
                updated = await mcp.call_tool_success(
                    "ha_set_todo_item",
                    {
                        "entity_id": LIST,
                        "item": first,
                        "status": "completed",
                        "move_after": "",
                    },
                )
                assert updated["todo_item"]["uid"] == created[first]
                assert updated["todo_item"]["status"] == "completed"
                listed = await mcp.call_tool_success("ha_get_todo", {"entity_id": LIST})
                assert _names(listed)[0] == first

                removed = await mcp.call_tool_success(
                    "ha_remove_todo_item",
                    {"entity_id": LIST, "item": [first, created[second]]},
                )
                assert {i["uid"] for i in removed["removed"]} == set(created.values())
            finally:
                # Core refuses a whole list when one item is gone.
                for summary in (first, second):
                    await safe_call_tool(
                        mcp_client,
                        "ha_remove_todo_item",
                        {"entity_id": LIST, "item": summary},
                    )


@pytest.mark.todo
@pytest.mark.requires_component_surface
async def test_component_resolves_with_cores_own_lookup(mcp_client, ha_client) -> None:
    """The todo_item bridge finds Core's todo parts on this Core: a missing
    one would answer ``unsupported`` and every write would fall back."""
    summary = f"e2e-bridge-{uuid.uuid4().hex[:8]}"
    async with MCPAssertions(mcp_client) as mcp:
        try:
            created = await mcp.call_tool_success(
                "ha_set_todo_item", {"entity_id": LIST, "summary": summary}
            )
            reply = await ha_client.send_websocket_message(
                {
                    "type": "ha_mcp_tools/todo_item",
                    "entity_id": LIST,
                    "action": "resolve",
                    "refs": [summary],
                }
            )
            assert reply.get("success") is True, reply
            assert reply["result"]["success"] is True, reply
            assert reply["result"]["items"][0]["uid"] == created["todo_item"]["uid"]
        finally:
            await safe_call_tool(
                mcp_client, "ha_remove_todo_item", {"entity_id": LIST, "item": summary}
            )
