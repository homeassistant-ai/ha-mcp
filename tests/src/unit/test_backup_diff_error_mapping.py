"""Dispatcher-level error-mapping tests for ``ha_manage_backup(action="diff")`` (#1950).

The ``(edits, diff)`` path maps exceptions raised by
``BackupManager.diff_snapshot`` onto tool-layer error codes:

    FileNotFoundError         -> RESOURCE_NOT_FOUND
    (ValueError, LookupError) -> VALIDATION_INVALID_PARAMETER
    TodoListNotLoaded         -> ENTITY_UNAVAILABLE
    ToolError                 -> re-raised unchanged (guard-pattern passthrough)
    any other Exception       -> exception_to_structured_error (INTERNAL_ERROR here)

Repo-wide this mapping had zero coverage: the unit suite drives
``BackupManager.diff_snapshot`` directly (below the dispatcher) and the e2e
suite exercises only the success path plus the pre-dispatch ``_require``
guard, so a reversed except-chain order or a changed error-code choice would
ship green. These tests call the real ``ha_manage_backup`` tool with
``action="diff"``; the server's own manager's ``diff_snapshot`` raises each
class.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import BackupManager
from ha_mcp.backup_todo import TodoListNotLoaded
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._backup_core import snapshot_manager as snapshot_manager

# A well-formed auto-backup name so the pre-dispatch ``_require`` guard passes
# and control reaches the ``diff_snapshot`` call under test.
_BNAME = "automation.kitchen_lights.20260521_153000.yaml"


async def _diff_raising(
    client: HomeAssistantClient,
    manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> ToolError:
    """The tool's error when the server's manager fails the diff with ``error``."""
    monkeypatch.setattr(manager, "diff_snapshot", AsyncMock(side_effect=error))
    with pytest.raises(ToolError) as caught:
        await manage_backup(client, scope="edits", action="diff", backup_name=_BNAME)
    return caught.value


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        pytest.param(
            FileNotFoundError("gone"),
            ErrorCode.RESOURCE_NOT_FOUND,
            id="file_not_found_to_resource_not_found",
        ),
        pytest.param(
            ValueError("bad snapshot"),
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            id="value_error_to_validation",
        ),
        # LookupError is the second member of the ``(ValueError, LookupError)``
        # tuple - a separate case so narrowing the tuple to ``ValueError`` alone
        # cannot ship green.
        pytest.param(
            LookupError("missing key"),
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            id="lookup_error_to_validation",
        ),
        pytest.param(
            TodoListNotLoaded("todo.tasks"),
            ErrorCode.ENTITY_UNAVAILABLE,
            id="todo_list_not_loaded_to_entity_unavailable",
        ),
        # A neutral message ("boom" matches no keyword bucket in
        # _classify_by_message) so the generic funnel lands on INTERNAL_ERROR
        # deterministically rather than a message-classified code.
        pytest.param(
            RuntimeError("boom"),
            ErrorCode.INTERNAL_ERROR,
            id="generic_to_structured_internal",
        ),
    ],
)
async def test_diff_error_mapping(
    core_client: HomeAssistantClient,
    snapshot_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_code: ErrorCode,
) -> None:
    error = await _diff_raising(core_client, snapshot_manager, monkeypatch, exc)
    assert expected_code.value in str(error)


async def test_diff_tool_error_passthrough_is_not_remapped(
    core_client: HomeAssistantClient,
    snapshot_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ToolError`` raised below the dispatcher must propagate unchanged -
    the ``except ToolError: raise`` guard must precede ``except Exception``,
    else the outer handler double-wraps it into INTERNAL_ERROR. Asserted by
    instance identity: a re-wrap would raise a NEW ``ToolError``."""
    sentinel = ToolError("sentinel-passthrough")
    error = await _diff_raising(core_client, snapshot_manager, monkeypatch, sentinel)
    assert error is sentinel
