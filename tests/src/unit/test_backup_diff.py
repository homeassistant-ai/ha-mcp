"""What ``ha_manage_backup(action="diff")`` reports, on real Core.

A config snapshot is compared as a JSON patch: the operations that turn what
Home Assistant holds now back into the snapshot. A file snapshot is compared
as a unified diff from the live file to the snapshot. Each test takes a
snapshot of what Core holds through the server's own client, changes Core or
the snapshot file, and reads the comparison through the tool. The unchanged,
renamed, deleted and over-the-cap cases of the patch, and the changed and
unchanged file, are in ``test_backup_manager.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import HEADER, manage_backup
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files
from ._helper_tool_core import create_item, set_up

FILE = "www/x.css"


async def _diff(client: HomeAssistantClient, snapshot: Path) -> dict[str, Any]:
    """The tool's answer for ``snapshot``."""
    result: dict[str, Any] = await manage_backup(
        client, scope="edits", action="diff", backup_name=snapshot.name
    )
    assert result["success"] is True, result
    return result


async def _select(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    manager: bm.BackupManager,
    options: list[str],
) -> tuple[str, Path]:
    """An input_select holding ``options``, and a snapshot of it."""
    await set_up(hass, "input_select")
    item_id = await create_item(client, "input_select", {"options": options})
    snapshot = await manager.maybe_snapshot("helper_input_select", item_id, force=True)
    assert snapshot is not None
    return item_id, snapshot


async def _set_options(
    client: HomeAssistantClient, snapshot: Path, item_id: str, options: list[str]
) -> None:
    """Give the select ``options``; its name stays the one ``snapshot`` holds."""
    updated = await client.send_websocket_message(
        {
            "type": "input_select/update",
            "input_select_id": item_id,
            "name": _held(snapshot)["name"],
            "options": options,
        }
    )
    assert updated["success"], updated


def _edit_snapshot(snapshot: Path, config: Any) -> None:
    """Make the snapshot hold ``config``, as a hand edit of the file would."""
    stored = yaml.safe_load(snapshot.read_text())
    stored["config"] = config
    snapshot.write_text(HEADER + yaml.safe_dump(stored, sort_keys=False))


def _held(snapshot: Path) -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(snapshot.read_text())["config"]
    return config


# --------------------------------------------------------------------------
# A config snapshot: the JSON patch back to it
# --------------------------------------------------------------------------


async def test_a_field_only_the_snapshot_holds_is_added_with_its_name_escaped(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """A field name holding ``/`` or ``~`` is one JSON Pointer token: both are
    escaped (RFC 6901), so the patch addresses that field and no other."""
    _, snapshot = await _select(hass, core_client, snapshot_manager, ["a", "b"])
    _edit_snapshot(snapshot, {**_held(snapshot), "a/b~c": 2})

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [{"op": "add", "path": "/a~1b~0c", "value": 2}]


async def test_a_field_only_core_holds_is_removed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    _, snapshot = await _select(hass, core_client, snapshot_manager, ["a", "b"])
    held = _held(snapshot)
    del held["options"]
    _edit_snapshot(snapshot, held)

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [{"op": "remove", "path": "/options"}]


async def test_true_and_1_are_a_difference(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Python counts ``True == 1``; a toggle holding 1 is still not one holding
    true, so the patch puts the snapshot's value back."""
    await set_up(hass, "input_boolean")
    item_id = await create_item(core_client, "input_boolean", {"initial": True})
    snapshot = await snapshot_manager.maybe_snapshot(
        "helper_input_boolean", item_id, force=True
    )
    assert snapshot is not None
    held = _held(snapshot)
    assert held["initial"] is True
    _edit_snapshot(snapshot, {**held, "initial": 1})

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [
        {"op": "replace", "path": "/initial", "value": 1}
    ]


async def test_entries_core_added_to_a_list_are_removed_last_first(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Each removal renumbers what follows it, so the highest index goes first
    and every operation still points at the entry it names."""
    item_id, snapshot = await _select(hass, core_client, snapshot_manager, ["a", "b"])
    await _set_options(core_client, snapshot, item_id, ["a", "b", "c", "d"])

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [
        {"op": "remove", "path": "/options/3"},
        {"op": "remove", "path": "/options/2"},
    ]


async def test_entries_core_dropped_from_a_list_are_appended(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    item_id, snapshot = await _select(
        hass, core_client, snapshot_manager, ["a", "b", "c"]
    )
    await _set_options(core_client, snapshot, item_id, ["a"])

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [
        {"op": "add", "path": "/options/-", "value": "b"},
        {"op": "add", "path": "/options/-", "value": "c"},
    ]


async def test_a_snapshot_of_another_shape_replaces_the_whole_config(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """When the snapshot holds a list where Core holds a mapping, no field can
    be patched: the one operation replaces the whole document (pointer ``""``)."""
    _, snapshot = await _select(hass, core_client, snapshot_manager, ["a", "b"])
    _edit_snapshot(snapshot, ["a", "b"])

    result = await _diff(core_client, snapshot)

    assert result["data"]["patch"] == [
        {"op": "replace", "path": "", "value": ["a", "b"]}
    ]


async def test_the_counts_tally_the_patch(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    item_id, snapshot = await _select(hass, core_client, snapshot_manager, ["a", "b"])
    await _set_options(core_client, snapshot, item_id, ["z", "b", "c", "d"])
    held = _held(snapshot)
    del held["name"]
    _edit_snapshot(snapshot, {**held, "extra": True})

    data = (await _diff(core_client, snapshot))["data"]

    ops = [op["op"] for op in data["patch"]]
    assert data["counts"] == {
        "add": ops.count("add"),
        "remove": ops.count("remove"),
        "replace": ops.count("replace"),
        "total": len(ops),
    }
    assert {"add", "remove", "replace"} <= set(ops)


async def _shrunk_select_patch(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    manager: bm.BackupManager,
    dropped: int,
) -> dict[str, Any]:
    """The diff of a select that lost ``dropped`` options: one add each."""
    options = [f"option {i}" for i in range(dropped + 1)]
    item_id, snapshot = await _select(hass, client, manager, options)
    await _set_options(client, snapshot, item_id, options[:1])
    return await _diff(client, snapshot)


async def test_a_patch_that_fills_the_cap_exactly_is_complete(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """A patch of exactly as many operations as the cap allows lost nothing,
    so it is neither flagged nor cut. The cap is read from a patch that
    overflowed it."""
    overflowed = await _shrunk_select_patch(hass, core_client, snapshot_manager, 1000)
    assert overflowed["data"]["truncated"] is True
    cap = len(overflowed["data"]["patch"])

    full = await _shrunk_select_patch(hass, core_client, snapshot_manager, cap)

    assert full["data"]["truncated"] is False
    assert len(full["data"]["patch"]) == cap
    assert "warnings" not in full


# --------------------------------------------------------------------------
# A file snapshot: the unified diff back to it
# --------------------------------------------------------------------------


async def _file_snapshot(
    hass: HomeAssistant, manager: bm.BackupManager, body: str
) -> Path:
    await write_config_files(hass, {FILE: body})
    snapshot = await manager.maybe_snapshot("file", FILE, force=True)
    assert snapshot is not None
    return snapshot


@pytest.mark.parametrize(
    ("stored", "current", "expected_changes"),
    [
        (
            "name: demo\n",
            "name: demo",
            "-name: demo\n\\ No newline at end of file\n+name: demo",
        ),
        (
            "name: demo",
            "name: demo\n",
            "-name: demo\n+name: demo\n\\ No newline at end of file",
        ),
        (
            "name: saved",
            "name: live",
            "-name: live\n\\ No newline at end of file\n+name: saved\n\\ No newline at end of file",
        ),
    ],
    ids=[
        "restore-final-newline",
        "remove-final-newline",
        "neither-has-final-newline",
    ],
)
async def test_a_missing_final_newline_shows_on_its_side_of_the_diff(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    stored: str,
    current: str,
    expected_changes: str,
) -> None:
    """A restore that adds or drops the final newline changes the file, so the
    diff shows it on the side that lacks one."""
    snapshot = await _file_snapshot(hass, file_snapshot_manager, stored)
    await write_config_files(hass, {FILE: current})

    data = (await _diff(core_client, snapshot))["data"]

    assert data["unchanged"] is False
    assert data["diff"] == (
        "--- current\n+++ snapshot\n@@ -1 +1 @@\n" + expected_changes
    )


async def test_a_unicode_line_separator_is_content_not_a_new_line(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    snapshot = await _file_snapshot(hass, file_snapshot_manager, "first\nsecond\n")
    await write_config_files(hass, {FILE: "first second\n"})

    data = (await _diff(core_client, snapshot))["data"]

    assert data["diff"] == (
        "--- current\n+++ snapshot\n@@ -1 +1,2 @@\n-first second\n+first\n+second"
    )


async def test_a_deleted_file_is_flagged_missing_not_unchanged(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """With no live file there is nothing to diff; the empty diff is no match."""
    snapshot = await _file_snapshot(hass, file_snapshot_manager, "x\n")
    await hass.async_add_executor_job(Path(hass.config.path(FILE)).unlink)

    result = await _diff(core_client, snapshot)

    assert result["data"]["entity_missing"] is True
    assert result["data"]["unchanged"] is False
    assert result["data"]["diff"] == ""
    assert any("missing" in warning for warning in result["warnings"])


async def _long_diff(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    manager: bm.BackupManager,
    stored: str,
) -> dict[str, Any]:
    """The diff from an empty file to ``stored``."""
    snapshot = await _file_snapshot(hass, manager, stored)
    await write_config_files(hass, {FILE: ""})
    return await _diff(client, snapshot)


async def test_a_long_diff_is_cut_and_says_so(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A whole-file rewrite must not flood the tool response."""
    stored = "".join(f"s{i}\n" for i in range(1000))

    result = await _long_diff(hass, core_client, file_snapshot_manager, stored)

    assert result["data"]["truncated"] is True
    assert len(result["data"]["diff"].splitlines()) < 1000
    assert any("truncated" in warning for warning in result["warnings"])


async def test_the_missing_newline_marker_counts_toward_the_cap(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A diff that fits the cap only without its no-final-newline marker is
    cut and flagged, not sent one line over. The cap is read from a diff that
    overflowed it."""
    overflowed = await _long_diff(
        hass,
        core_client,
        file_snapshot_manager,
        "".join(f"s{i}\n" for i in range(1000)),
    )
    cap = len(overflowed["data"]["diff"].split("\n"))
    # Three header lines and one line per stored line fill the cap; the
    # marker for the missing final newline is one line more.
    stored = "\n".join(f"line{i}" for i in range(cap - 3))

    result = await _long_diff(hass, core_client, file_snapshot_manager, stored)

    assert result["data"]["truncated"] is True
    assert len(result["data"]["diff"].split("\n")) <= cap
