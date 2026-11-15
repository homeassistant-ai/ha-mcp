"""``@with_auto_backup``: the pre-write capture a wrapped tool runs first.

The decorator reads the server's real settings and captures through the real
backup manager of the client it is given, so the snapshot a test finds is what
a write would have left behind. A ``mandatory`` tool fails closed: it is blocked,
unrun, when its snapshot cannot be taken.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.auto_backup import (
    automation_backup_target,
    refusal_behind_capture_failure,
    with_auto_backup,
)

from ._backup_core import (
    add_label,
    break_backup_dir,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files

TOOLS = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"entity_id": "automation.foo", "config": {"id": "BBB", "alias": "x"}},
            "BBB",
            id="config_id_wins_over_entity_id",
        ),
        pytest.param(
            {"entity_id": "automation.foo", "config": {"alias": "x"}},
            "automation.foo",
            id="entity_id_form_is_kept",
        ),
        pytest.param(
            {"entity_id": "my_unique_id", "config": {"alias": "x"}},
            "my_unique_id",
            id="bare_config_id_is_unchanged",
        ),
        pytest.param(
            {"entity_id": "automation.foo", "config": '{"id": "BBB", "alias": "x"}'},
            "BBB",
            id="config_as_json_string",
        ),
        pytest.param(
            {"entity_id": "automation.foo", "config": "not-json"},
            "automation.foo",
            id="invalid_json_falls_back_to_entity_id",
        ),
        pytest.param(
            {"config": {"alias": "x"}}, "", id="create_has_nothing_to_back_up"
        ),
    ],
)
def test_the_automation_backup_target_is_the_entity_the_write_will_overwrite(
    kwargs: dict[str, Any], expected: str
) -> None:
    """Home Assistant stores an automation by the body's ``id``, not the one in
    the URL (#1404), so the backup captures the entity at ``config.id``. The
    entity id keeps its ``automation.`` prefix: the fetch resolves it to the
    numeric id only when the prefix is there, and a bare slug would 404 and
    skip the snapshot silently."""
    assert automation_backup_target(kwargs) == expected


def _tool(client: Any = None, **options: Any) -> tuple[Any, list[str]]:
    """A wrapped write on ``client`` that records the paths it ran for."""
    ran: list[str] = []

    @with_auto_backup(domain="file", id_param="path", client=client, **options)
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    return tool, ran


async def test_a_wrapped_write_runs_after_its_target_is_snapshotted(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    ran: list[str] = []

    @with_auto_backup(domain="label", id_param="label_id", client=core_client)
    async def fake_tool(*, label_id: str) -> str:
        ran.append(label_id)
        return "ok"

    assert await fake_tool(label_id=kitchen) == "ok"

    assert ran == [kitchen]
    (row,) = snapshot_manager.list_snapshots(domain="label", entity_id=kitchen)
    assert snapshot_manager.read_snapshot(row["name"])["tool"] == "fake_tool"


async def test_a_capture_that_gets_no_answer_does_not_block_the_write(
    unreachable_client: HomeAssistantClient,
) -> None:
    """Capture is best-effort: a capture error must not block the write."""
    ran: list[str] = []

    @with_auto_backup(domain="label", id_param="label_id", client=unreachable_client)
    async def fake_tool(*, label_id: str) -> str:
        ran.append(label_id)
        return "ok"

    assert await fake_tool(label_id="kitchen") == "ok"
    assert ran == ["kitchen"]


def test_destructive_tools_carry_the_auto_backup_decorator() -> None:
    """Source guard: a deleted decorator line would silently stop capturing
    without failing any behavioural test."""
    entity_key, device_key = "id_fn=_single_entity_target", 'id_param="device_id"'
    # (file, tool, domain, key, mandatory); file and YAML writes are mandatory.
    checks = [
        ("tools_entities.py", "ha_set_entity", "entity", entity_key, False),
        ("tools_entities.py", "ha_remove_entity", "entity", entity_key, False),
        ("tools_registry.py", "ha_set_device", "device", device_key, False),
        ("tools_registry.py", "ha_remove_device", "device", device_key, False),
        ("tools_filesystem.py", "ha_write_file", "file", 'id_param="path"', True),
        ("tools_filesystem.py", "ha_delete_file", "file", 'id_param="path"', True),
        ("tools_yaml_config.py", "ha_config_set_yaml", "yaml", None, True),
    ]
    for fname, func, domain, key, mandatory in checks:
        src = (TOOLS / fname).read_text(encoding="utf-8")
        idx = src.index(f"async def {func}(")
        head = src[max(0, idx - 600) : idx]  # the decorator block above the def
        assert "with_auto_backup(" in head, f"{func} lost @with_auto_backup"
        assert f'domain="{domain}"' in head, f"{func} wrong/missing backup domain"
        if key is not None:
            assert key in head, f"{func} wrong/missing {key}"
        if mandatory:
            assert "mandatory=True" in head, f"{func} lost mandatory=True"


def _error_code(exc: ToolError) -> str:
    return str(json.loads(str(exc))["error"]["code"])


async def test_a_mandatory_tool_is_refused_while_auto_backup_is_off(
    backup_settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    tool, ran = _tool(mandatory=True)

    with pytest.raises(ToolError) as exc:
        await tool(path="www/x.css")

    # The code and the "blocked, nothing changed" guarantee are what the user
    # relies on, so a reword cannot silently gut them.
    assert _error_code(exc.value) == "CONFIG_VALIDATION_FAILED"
    assert "requires auto-backup" in str(exc.value)
    assert "blocked and nothing was changed" in str(exc.value)
    assert ran == []


async def test_a_mandatory_tool_without_a_client_to_capture_with_fails_closed(
    backup_settings: config.Settings,
) -> None:
    """Writing un-backed-up is what the toggle being on promised not to do."""
    tool, ran = _tool(mandatory=True)

    with pytest.raises(ToolError) as exc:
        await tool(path="www/x.css")

    assert _error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
    assert ran == []


async def test_a_non_mandatory_tool_proceeds_while_auto_backup_is_off(
    backup_settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    tool, ran = _tool(mandatory=False)

    assert await tool(path="www/x.css") == "wrote"
    assert ran == ["www/x.css"]


async def test_a_capture_that_fails_blocks_a_mandatory_write_with_the_reason(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An existing file whose snapshot cannot be written (an unusable backup
    directory) is not overwritten un-backed-up, and the error carries the
    remediation."""
    await write_config_files(hass, {"www/x.css": "old"})
    break_backup_dir(backup_settings, monkeypatch, tmp_path)
    tool, ran = _tool(core_client, mandatory=True)

    with pytest.raises(ToolError) as exc:
        await tool(path="www/x.css")

    assert _error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
    assert "unusable" in str(exc.value)
    assert "blocked and nothing was changed" in str(exc.value)
    assert ran == []


async def test_a_missing_file_tools_entry_is_reported_as_that_not_as_a_backup_failure(
    core_client: HomeAssistantClient, snapshot_manager: bm.BackupManager
) -> None:
    """The capture failed because the File & YAML Tools entry is absent, so the
    remediation is "add the entry" and retrying the backup cannot succeed
    (#2292). The write stays blocked either way."""
    tool, ran = _tool(core_client, mandatory=True)

    with pytest.raises(ToolError) as exc:
        await tool(path="www/x.css")

    assert _error_code(exc.value) == "COMPONENT_NOT_INSTALLED"
    assert ran == []


async def test_a_new_file_is_a_legitimate_skip_and_the_write_proceeds(
    core_client: HomeAssistantClient, file_snapshot_manager: bm.BackupManager
) -> None:
    """Nothing to snapshot for a file that does not exist yet is not a failure."""
    tool, ran = _tool(core_client, mandatory=True)

    assert await tool(path="www/new.css") == "wrote"
    assert ran == ["www/new.css"]


async def test_a_target_that_cannot_be_resolved_aborts_the_write(
    backup_settings: config.Settings, core_client: HomeAssistantClient
) -> None:
    """Target resolution sits outside the best-effort capture handler, so an
    error in it aborts the call rather than letting an un-backed-up write
    through."""
    ran: list[str] = []

    def unresolvable(_kw: dict[str, Any]) -> str:
        raise HomeAssistantError("cannot resolve backup target")

    @with_auto_backup(
        domain="file", id_fn=unresolvable, mandatory=True, client=core_client
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    with pytest.raises(HomeAssistantError):
        await tool(path="www/x.css")

    assert ran == []


def _structured(code: ErrorCode, message: str) -> ToolError:
    return ToolError(json.dumps(create_error_response(code, message)))


def test_a_capture_failure_caused_by_a_missing_entry_is_found_behind_another_cause() -> (
    None
):
    """``raise ... from other`` buries the component error in ``__context__``
    while an unrelated exception occupies ``__cause__``; the walk must follow
    both links or the failure reads as a backup failure again."""
    component_error = _structured(ErrorCode.COMPONENT_NOT_INSTALLED, "Add the entry")
    try:
        try:
            raise component_error
        except ToolError:
            raise bm.MandatoryBackupError("capture failed") from RuntimeError("other")
    except bm.MandatoryBackupError as failure:
        assert refusal_behind_capture_failure(failure) is component_error


def test_a_structured_error_with_another_code_is_not_unwrapped() -> None:
    """The walk matches on the code, not on "a ToolError is in there somewhere":
    unwrapping an unrelated one would hand the caller a remediation that has
    nothing to do with why the write was blocked."""
    unrelated = _structured(ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS, "cannot read")
    failure = bm.MandatoryBackupError("could not read the current state")
    failure.__cause__ = unrelated

    assert refusal_behind_capture_failure(failure) is None


def test_a_plain_text_tool_error_in_the_chain_is_tolerated() -> None:
    """Not every ToolError carries a JSON body."""
    failure = bm.MandatoryBackupError("could not read the current state")
    failure.__cause__ = ToolError("something went wrong")

    assert refusal_behind_capture_failure(failure) is None
