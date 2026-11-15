"""Todo item backups (``ha_mcp.backup_todo``): snapshot and restore payloads.

The list is Core's Local To-do list, or a list of a test platform where the
test needs a backend Local To-do is not (no registry entry, an unavailable
list, a backend that fails or adds rows of its own). Core's todo services act
on it, so each test reads the rows a restore leaves and the service data Core
received.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError as CoreError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.backup_helper_target import note_no_safety_backup
from ha_mcp.backup_helper_target import safety_target as restore_safety_target
from ha_mcp.backup_todo import (
    TodoListNotLoaded,
    current_key,
    fetch_todo_item,
    restore_todo_item,
    safety_target,
)
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantError,
)

from ._backup_core import backup_settings as backup_settings
from ._backup_core import place_snapshot
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_todo_core import (
    LIST,
    PLAIN_FEATURES,
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
from ._backup_todo_core import todo_core as todo_core

OPEN = TodoItemStatus.NEEDS_ACTION


async def test_an_item_is_found_by_its_summary_or_its_uid(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Buy milk")
    expected = {
        "todo_entity_id": LIST,
        "uid": uid,
        "summary": "Buy milk",
        "status": "needs_action",
    }
    assert await fetch_todo_item(core_client, f"{LIST}::Buy milk") == expected
    assert await fetch_todo_item(core_client, f"{LIST}::{uid}") == expected


async def test_an_item_the_list_does_not_hold_has_nothing_to_back_up(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    await add_item(hass, "Buy milk")
    assert await fetch_todo_item(core_client, f"{LIST}::Bread") is None
    assert await fetch_todo_item(core_client, f"{LIST}::") is None


async def test_a_summary_two_items_share_is_the_first_one(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    """Core's own update_item acts on the first match, so the backup is of it."""
    first = await add_item(hass, "Pay")
    second = await add_item(hass, "Pay")
    assert first != second

    assert (await item_snapshot(core_client, "Pay"))["uid"] == first
    await write_item(hass, "update_item", "Pay", status="completed")
    rows = await listed(core_client)
    assert [row["status"] for row in rows] == ["completed", "needs_action"]


async def test_a_uid_wins_over_an_item_named_like_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    pay = await add_item(hass, "Pay")
    other = await add_item(hass, pay)
    await move_to_top(hass, other)

    assert (await item_snapshot(core_client, pay))["summary"] == "Pay"


async def test_a_list_core_has_not_loaded_is_not_read_as_a_missing_item(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    """Whether the item is on a list Core has not loaded is unknown."""
    await add_item(hass, "Pay")
    assert await hass.config_entries.async_unload(tasks.entry_id)
    with pytest.raises(TodoListNotLoaded):
        await fetch_todo_item(core_client, f"{LIST}::Pay")


@pytest.mark.usefixtures("todo_core")
async def test_a_list_with_a_state_but_no_entity_is_not_read_as_a_missing_item(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    hass.states.async_set("todo.legacy", "2")
    with pytest.raises(TodoListNotLoaded):
        await fetch_todo_item(core_client, "todo.legacy::Pay")


@pytest.mark.usefixtures("todo_core")
async def test_a_list_that_never_existed_has_nothing_to_back_up(
    core_client: HomeAssistantClient,
) -> None:
    assert await fetch_todo_item(core_client, "todo.gone::Bread") is None


@pytest.mark.usefixtures("todo_core")
async def test_a_list_error_that_is_not_a_miss_is_raised(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(HomeAssistantCommandError):
        await fetch_todo_item(core_client, "not a list::Pay")


async def test_an_edited_item_is_written_back_under_its_uid(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Pay", completed=True, due_date="2026-11-01")
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(
        hass,
        "update_item",
        uid,
        rename="Paid",
        status="needs_action",
        description="added",
        due_datetime="2026-12-24T10:00:00+01:00",
    )
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    row = row_named(await listed(core_client), "Pay")
    assert (row["uid"], row["status"], row["due"]) == (uid, "completed", "2026-11-01")
    assert "description" not in row
    assert sent() == [
        (
            "update_item",
            {
                "item": uid,
                "rename": "Pay",
                "status": "completed",
                "description": None,
                "due_date": "2026-11-01",
                "entity_id": [LIST],
            },
        )
    ]


async def test_a_due_date_the_snapshot_did_not_have_is_cleared(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Buy milk")
    snapshot = await item_snapshot(core_client, "Buy milk")
    await write_item(hass, "update_item", uid, due_datetime="2026-12-24T10:00:00+01:00")
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Buy milk", snapshot)

    assert "due" not in row_named(await listed(core_client), "Buy milk")
    assert sent()[-1][1]["due_datetime"] is None


async def test_a_cleared_description_is_restored(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Buy milk", description="two litres")
    snapshot = await item_snapshot(core_client, "Buy milk")
    await write_item(hass, "update_item", uid, description="")

    await restore_todo_item(core_client, f"{LIST}::Buy milk", snapshot)

    row = row_named(await listed(core_client), "Buy milk")
    assert row["description"] == "two litres"


async def test_no_description_or_due_is_sent_to_a_list_that_has_none(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """Core refuses description and due on a list without those features, so
    the restore leaves them out when neither the snapshot nor the item has one."""
    plain_list.features = PLAIN_FEATURES
    plain_list.async_write_ha_state()
    snapshot = await item_snapshot(core_client, "Buy milk")
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Buy milk", snapshot)

    assert sent() == [
        (
            "update_item",
            {
                "item": "1",
                "rename": "Buy milk",
                "status": "needs_action",
                "entity_id": [LIST],
            },
        )
    ]


async def test_a_removed_completed_item_is_added_again_then_completed(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    old = await add_item(hass, "Pay", completed=True, due_date="2026-11-01")
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", old)
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    new = uid_of(hass, "Pay")
    assert new != old
    assert sent() == [
        ("add_item", {"item": "Pay", "due_date": "2026-11-01", "entity_id": [LIST]}),
        ("update_item", {"item": new, "status": "completed", "entity_id": [LIST]}),
    ]
    row = row_named(await listed(core_client), "Pay")
    assert (row["status"], row["due"]) == ("completed", "2026-11-01")


async def test_a_removed_open_item_needs_no_status_call(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    old = await add_item(hass, "Buy milk")
    snapshot = await item_snapshot(core_client, "Buy milk")
    await write_item(hass, "remove_item", old)
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Buy milk", snapshot)

    assert sent() == [("add_item", {"item": "Buy milk", "entity_id": [LIST]})]
    assert row_named(await listed(core_client), "Buy milk")["status"] == "needs_action"


async def test_an_added_item_that_cannot_be_told_apart_is_not_completed(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """Another writer's row lands beside the re-added one, so completing "the
    new item" could change the wrong row."""
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.created_as = lambda item: [
        item,
        TodoItem(uid="x", summary="other", status=OPEN),
    ]
    sent = watch_services(hass)

    with pytest.raises(HomeAssistantError, match="could not be told apart"):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert [service for service, _ in sent()] == ["add_item"]
    assert all(row.status == OPEN for row in plain_list.rows if row.uid != "2")


async def test_an_added_item_another_writer_replaced_is_not_completed(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.created_as = lambda item: [dataclasses.replace(item, summary="other")]

    with pytest.raises(HomeAssistantError, match="could not be told apart"):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert plain_list.rows[-1].status == OPEN


async def test_an_added_item_without_a_uid_is_not_completed(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.created_as = lambda item: [dataclasses.replace(item, uid=None)]
    sent = watch_services(hass)

    with pytest.raises(HomeAssistantError, match="could not be told apart"):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert [service for service, _ in sent()] == ["add_item"]


async def test_an_item_named_like_the_new_uid_blocks_the_completion(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """Core's update_item would change the earlier row named "101", not the new one."""
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.rows.insert(0, TodoItem(uid="z", summary="101", status=OPEN))
    sent = watch_services(hass)

    with pytest.raises(HomeAssistantError, match="could not be told apart"):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert [service for service, _ in sent()] == ["add_item"]
    assert plain_list.rows[0].status == OPEN


@pytest.mark.usefixtures("todo_core")
async def test_restoring_to_a_list_that_is_gone_says_so(
    core_client: HomeAssistantClient,
) -> None:
    snapshot = {"todo_entity_id": "todo.gone", "uid": "1", "summary": "x"}
    with pytest.raises(bm.BackupRestoreError, match="no longer exists") as refused:
        await restore_todo_item(core_client, "todo.gone::x", snapshot)
    assert refused.value.outcome["apply_status"] == "not_applied"
    assert refused.value.outcome["reason"] == "entity_not_found"


async def test_an_unavailable_list_is_refused_before_anything_is_written(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """Core skips an unavailable list and still answers success."""
    snapshot = await item_snapshot(core_client, "Pay")
    plain_list.is_available = False
    plain_list.async_write_ha_state()
    sent = watch_services(hass)

    with pytest.raises(bm.BackupRestoreError, match="unavailable") as refused:
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert refused.value.outcome["apply_status"] == "not_applied"
    assert refused.value.outcome["reason"] == "entity_unavailable"
    assert sent() == []


async def test_a_list_without_a_registry_entry_is_restored(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    plain_list: PlainList,
    entity_registry: er.EntityRegistry,
) -> None:
    """A list set up without a unique id has a state but no registry entry."""
    assert entity_registry.async_get(LIST) is None
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "update_item", "2", rename="Paid")

    await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert [row.summary for row in plain_list.rows] == ["Buy milk", "Pay"]


async def test_an_unloaded_list_is_refused_as_not_applied(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    await add_item(hass, "Pay")
    snapshot = await item_snapshot(core_client, "Pay")
    assert await hass.config_entries.async_unload(tasks.entry_id)
    sent = watch_services(hass)

    with pytest.raises(bm.BackupRestoreError, match="unavailable") as refused:
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)
    with pytest.raises(bm.BackupRestoreError) as safety:
        await safety_target(core_client, f"{LIST}::Pay", snapshot)

    for error in (refused.value, safety.value):
        assert error.outcome["apply_status"] == "not_applied"
        assert error.outcome["reason"] == "entity_unavailable"
    assert sent() == []


@pytest.mark.usefixtures("todo_core")
async def test_a_list_with_a_state_but_no_entity_is_refused_as_not_applied(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    hass.states.async_set("todo.legacy", "2")
    snapshot = {"todo_entity_id": "todo.legacy", "uid": "1", "summary": "Pay"}
    sent = watch_services(hass)

    with pytest.raises(
        bm.BackupRestoreError, match="has not loaded the todo list"
    ) as refused:
        await restore_todo_item(core_client, "todo.legacy::Pay", snapshot)
    with pytest.raises(bm.BackupRestoreError) as safety:
        await safety_target(core_client, "todo.legacy::Pay", snapshot)

    for error in (refused.value, safety.value):
        assert error.outcome["apply_status"] == "not_applied"
        assert error.outcome["reason"] == "entity_unavailable"
    assert sent() == []


async def test_a_snapshot_without_a_uid_updates_the_item_by_summary(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    await add_item(hass, "Pay", completed=True)
    snapshot = {"todo_entity_id": LIST, "summary": "Pay", "status": "needs_action"}
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert sent() == [
        (
            "update_item",
            {
                "item": "Pay",
                "rename": "Pay",
                "status": "needs_action",
                "entity_id": [LIST],
            },
        )
    ]
    rows = await listed(core_client)
    assert [(row["summary"], row["status"]) for row in rows] == [
        ("Pay", "needs_action")
    ]


async def test_a_failed_status_step_says_the_item_was_added(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """A retry would add the item twice, so the error says it was added."""
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "remove_item", "2")
    plain_list.update_failure = CoreError("backend down")

    with pytest.raises(HomeAssistantError, match=r"Re-added 'Pay'.*no answer"):
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert plain_list.rows[-1].summary == "Pay"


async def test_a_snapshot_leaves_out_the_completed_time(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    await add_item(hass, "Pay", completed=True)
    assert "completed" in row_named(await listed(core_client), "Pay")

    assert "completed" not in await item_snapshot(core_client, "Pay")


async def test_the_list_comes_from_the_backup_key(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Pay", completed=True)
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "update_item", uid, status="needs_action")

    await restore_todo_item(
        core_client, f"{LIST}::Pay", {**snapshot, "todo_entity_id": "todo.other"}
    )

    assert row_named(await listed(core_client), "Pay")["status"] == "completed"


async def test_an_item_named_like_the_uid_blocks_the_restore(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    """Core's update_item would change the earlier row named after the uid."""
    pay = await add_item(hass, "Pay", completed=True)
    snapshot = await item_snapshot(core_client, "Pay")
    await write_item(hass, "update_item", pay, status="needs_action")
    await move_to_top(hass, await add_item(hass, pay))
    sent = watch_services(hass)

    with pytest.raises(bm.BackupRestoreError, match="nothing was restored") as refused:
        await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert refused.value.outcome["apply_status"] == "not_applied"
    assert sent() == []


def test_the_current_key_uses_the_uid_where_the_snapshot_has_one() -> None:
    assert current_key(f"{LIST}::Pay", {"uid": "2"}) == f"{LIST}::2"
    assert current_key(f"{LIST}::Pay", {"summary": "Pay"}) == f"{LIST}::Pay"


async def test_a_safety_backup_follows_the_item_by_its_uid(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    """A backup keyed by the old summary finds the renamed item by its uid."""
    uid = await add_item(hass, "Pay")
    await write_item(hass, "update_item", uid, rename="Paid")

    target = await restore_safety_target(
        core_client, "todo_item", f"{LIST}::Pay", {"uid": uid}
    )

    assert target == f"{LIST}::{uid}"


async def test_no_safety_backup_is_taken_for_a_removed_item(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    uid = await add_item(hass, "Pay")
    await write_item(hass, "remove_item", uid)

    assert await safety_target(core_client, f"{LIST}::Pay", {"uid": uid}) is None


async def test_a_snapshot_without_a_uid_is_the_item_named_so(
    hass: HomeAssistant, core_client: HomeAssistantClient, tasks: MockConfigEntry
) -> None:
    await add_item(hass, "Pay")
    item = {"summary": "Pay", "status": "needs_action"}
    assert await safety_target(core_client, f"{LIST}::Pay", item) == f"{LIST}::Pay"


async def test_a_row_named_after_a_removed_items_uid_is_not_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    tasks: MockConfigEntry,
) -> None:
    """The restore adds the item again and leaves that row alone, so it takes
    no safety backup of it and the diff finds the item missing."""
    uid = await add_item(hass, "Pay")
    path = await snapshot_manager.maybe_snapshot("todo_item", f"{LIST}::{uid}")
    assert path is not None
    await write_item(hass, "remove_item", uid)
    await add_item(hass, uid)

    assert await safety_target(core_client, f"{LIST}::{uid}", {"uid": uid}) is None
    result = await snapshot_manager.diff_snapshot(path.name)
    assert result["entity_missing"] is True


async def test_a_diff_reads_the_item_under_its_uid(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    tasks: MockConfigEntry,
) -> None:
    uid = await add_item(hass, "Pay")
    path = await snapshot_manager.maybe_snapshot("todo_item", f"{LIST}::Pay")
    assert path is not None
    await write_item(hass, "update_item", uid, rename="Paid")

    result = await snapshot_manager.diff_snapshot(path.name)

    assert result["kind"] == "dict"
    assert result["entity_missing"] is False
    assert {"op": "replace", "path": "/summary", "value": "Pay"} in result["patch"]


async def test_a_diff_ignores_the_completed_time_an_older_snapshot_stored(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    tasks: MockConfigEntry,
) -> None:
    """Snapshots from before this change stored the list's completion time."""
    await add_item(hass, "Pay", completed=True)
    path = await snapshot_manager.maybe_snapshot("todo_item", f"{LIST}::Pay")
    assert path is not None
    held = snapshot_manager.read_snapshot(path.name)["config"]
    place_snapshot(
        path.parent,
        path.name,
        f"{LIST}::Pay",
        domain="todo_item",
        held={**held, "completed": "2026-10-01T10:00:00+00:00"},
        text=False,
    )

    result = await snapshot_manager.diff_snapshot(path.name)

    assert result["unchanged"] is True


async def test_the_warning_for_a_removed_item_names_its_summary() -> None:
    outcome: dict[str, object] = {}
    note_no_safety_backup(outcome, "todo_item", None, {"uid": "2", "summary": "Pay"})
    assert outcome["warnings"] == [
        "No safety backup was taken: todo_item Pay no longer exists, "
        "so this restore overwrites none of its state."
    ]


async def test_an_item_added_again_is_completed_by_its_new_uid(
    hass: HomeAssistant, core_client: HomeAssistantClient, plain_list: PlainList
) -> None:
    """The snapshot's uid is gone, so the item is added again; an older row of
    the same summary is not the one completed."""
    snapshot = {**await item_snapshot(core_client, "Pay"), "uid": "gone"}
    await write_item(hass, "update_item", "2", status="needs_action")
    sent = watch_services(hass)

    await restore_todo_item(core_client, f"{LIST}::Pay", snapshot)

    assert sent()[-1] == (
        "update_item",
        {"item": "101", "status": "completed", "entity_id": [LIST]},
    )
    assert [
        (row.uid, row.status) for row in plain_list.rows if row.summary == "Pay"
    ] == [
        ("2", OPEN),
        ("101", TodoItemStatus.COMPLETED),
    ]
    assert plain_list.rows[-1].due == date(2026, 11, 1)
