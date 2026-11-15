"""Todo item restores through the component's ``todo_item`` bridge.

With the component, a restore writes by uid and hands Core's own date
validator the snapshot's due as listed. The component runs on the same real
``hass`` as Core's todo services, so each test reads the rows a restore leaves
and the service data Core received.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.backup_todo import fetch_todo_item, restore_todo_item
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantError,
)

from ._backup_todo_core import (
    LIST,
    PlainList,
    add_item,
    item_snapshot,
    listed,
    move_to_top,
    row_named,
    uid_of,
    watch_services,
    write_item,
)
from ._backup_todo_core import plain_list as plain_list
from ._backup_todo_core import tasks as tasks
from ._command_faults import unanswered

BRIDGE = "ha_mcp_tools/todo_item"


async def test_an_item_is_found_by_its_summary_through_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
) -> None:
    uid = await add_item(hass, "Buy milk")
    got = await fetch_todo_item(component_core_client, f"{LIST}::Buy milk")
    assert got is not None
    assert got["uid"] == uid


async def test_an_edited_item_is_updated_by_uid(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
) -> None:
    uid = await add_item(hass, "Pay", completed=True, due_date="2026-11-01")
    snapshot = await item_snapshot(component_core_client, "Pay")
    await write_item(hass, "update_item", uid, rename="Paid", status="needs_action")
    sent = watch_services(hass)

    await restore_todo_item(component_core_client, f"{LIST}::Pay", snapshot)

    assert sent() == [
        (
            "update_item",
            {
                "item": uid,
                "rename": "Pay",
                "status": "completed",
                "due_date": "2026-11-01",
                "entity_id": LIST,
            },
        )
    ]
    row = row_named(await listed(component_core_client), "Pay")
    assert (row["uid"], row["status"]) == (uid, "completed")


async def test_a_removed_completed_item_is_added_then_completed(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
) -> None:
    old = await add_item(hass, "Pay", completed=True, due_date="2026-11-01")
    snapshot = await item_snapshot(component_core_client, "Pay")
    await write_item(hass, "remove_item", old)
    sent = watch_services(hass)

    await restore_todo_item(component_core_client, f"{LIST}::Pay", snapshot)

    new = uid_of(hass, "Pay")
    assert sent() == [
        ("add_item", {"item": "Pay", "due_date": "2026-11-01", "entity_id": LIST}),
        ("update_item", {"item": new, "status": "completed", "entity_id": LIST}),
    ]
    assert (
        row_named(await listed(component_core_client), "Pay")["status"] == "completed"
    )


@pytest.mark.parametrize("removed", [False, True], ids=["edited", "removed"])
async def test_a_rejected_token_does_not_fall_back_to_cores_services(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
    removed: bool,
) -> None:
    """Core's todo services would connect again with the rejected token."""
    uid = await add_item(hass, "Pay", completed=True)
    snapshot = await item_snapshot(core_client, "Pay")
    if removed:
        await write_item(hass, "remove_item", uid)
    unsent = HomeAssistantCommandNotSent("rejected")
    unsent.__cause__ = HomeAssistantAuthError("WebSocket authentication failed")
    sent = watch_services(hass)

    with (
        patch("ha_mcp.backup_todo.todo_item", AsyncMock(side_effect=unsent)),
        pytest.raises(HomeAssistantCommandNotSent),
    ):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert sent() == []


async def test_a_refusal_is_reported_as_not_applied(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
) -> None:
    """The component refuses an update that Core would make to another item."""
    uid = await add_item(hass, "Pay", completed=True)
    snapshot = await item_snapshot(component_core_client, "Pay")
    await write_item(hass, "update_item", uid, rename="Paid", status="needs_action")
    await move_to_top(hass, await add_item(hass, uid))
    sent = watch_services(hass)

    with pytest.raises(bm.BackupRestoreError, match="Another item") as refused:
        await restore_todo_item(component_core_client, f"{LIST}::Pay", snapshot)

    assert refused.value.outcome["apply_status"] == "not_applied"
    assert row_named(await listed(component_core_client), "Paid")["uid"] == uid
    assert sent() == []


async def test_a_re_added_item_the_component_cannot_tell_apart_is_reported_as_added(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    plain_list: PlainList,
) -> None:
    """A retry would add the item twice, so this is no not-applied refusal."""
    snapshot = await item_snapshot(component_core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.created_as = lambda item: [
        item,
        TodoItem(uid="x", summary="other", status=TodoItemStatus.NEEDS_ACTION),
    ]
    sent = watch_services(hass)

    with pytest.raises(HomeAssistantError, match="Re-added 'Pay'") as added:
        await restore_todo_item(component_core_client, f"{LIST}::Pay", snapshot)

    assert not isinstance(added.value, bm.BackupRestoreError)
    assert [service for service, _ in sent()] == ["add_item"]


async def test_a_write_without_an_answer_is_no_refusal(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tasks: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await add_item(hass, "Pay", completed=True)
    snapshot = await item_snapshot(component_core_client, "Pay")
    await write_item(hass, "update_item", uid, status="needs_action")

    async with unanswered(hass, BRIDGE, monkeypatch):
        with pytest.raises(HomeAssistantError) as lost:
            await restore_todo_item(component_core_client, f"{LIST}::Pay", snapshot)

    assert not isinstance(lost.value, bm.BackupRestoreError)
