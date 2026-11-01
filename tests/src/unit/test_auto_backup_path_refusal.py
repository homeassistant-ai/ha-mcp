"""A path the component refuses is reported as a path refusal (#2908).

The mandatory pre-write capture reads the file through the component's read
gate, which is wider than the write gate, so a refused path fails there first.
That must surface as the path refusal, not as a retryable
``BACKUP_CAPTURE_FAILED``, and without the read allowlist. Diffing, restoring
or re-capturing a snapshot of such a path says the live file is unreadable.
Kept out of ``test_backup_manager.py``, which is at its module-size baseline.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import MandatoryBackupError
from ha_mcp.tools import auto_backup
from ha_mcp.tools.auto_backup import PathNotAllowedError, with_auto_backup
from ha_mcp.tools.backup import _edits_diff, _edits_restore
from ha_mcp.tools.backup_on_demand import capture_on_demand

from .test_backup_manager import _StubClient, _StubSettings

READ_REFUSAL = "Path not allowed. Allowed paths: www/**, blueprints/**"


def _payload(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))


async def test_refused_path_write_reports_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _StubSettings(auto_backup_dir=str(tmp_path / "backups"))
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )

    async def fake_call(_client: Any, service: str, data: dict[str, Any]) -> Any:
        assert service == "read_file"
        return {"success": False, "error": READ_REFUSAL}

    monkeypatch.setattr(
        "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", fake_call
    )
    ran: list[str] = []

    @with_auto_backup(
        domain="file", id_param="path", mandatory=True, client=_StubClient()
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    with pytest.raises(ToolError) as exc:
        await tool(path="/backup/x.txt")

    payload = _payload(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["error"]["message"] == "Path not allowed: /backup/x.txt"
    assert payload["path"] == "/backup/x.txt"
    assert ran == []  # still blocked


@pytest.mark.parametrize(
    ("path", "offers_custom_path"),
    [
        ("pyscript/x.py", True),
        ("/share/notes.md", True),
        ("/backup/x.txt", False),
        (".storage/auth", False),
    ],
)
async def test_custom_path_is_suggested_only_where_it_can_be_added(
    path: str, offers_custom_path: bool
) -> None:
    error = json.loads(str(PathNotAllowedError(path, READ_REFUSAL)))["error"]

    assert ("add it" in json.dumps(error)) is offers_custom_path


class _StubManager:
    def __init__(self, err: BaseException) -> None:
        self._err = err
        self._client = None

    async def _raise(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._err

    diff_snapshot = restore_snapshot = maybe_snapshot = _raise


def _capture_failure() -> MandatoryBackupError:
    # How maybe_snapshot(mandatory=True) wraps a refused live-file read.
    try:
        raise PathNotAllowedError("/backup/x.txt", READ_REFUSAL)
    except PathNotAllowedError as refusal:
        try:
            raise MandatoryBackupError("could not back up") from refusal
        except MandatoryBackupError as err:
            return err


@pytest.mark.parametrize(
    ("action", "err"),
    [
        ("diff", PathNotAllowedError("/backup/x.txt", READ_REFUSAL)),
        ("restore", _capture_failure()),
    ],
)
async def test_snapshot_of_a_refused_path_says_the_live_file_is_unreadable(
    action: str, err: BaseException
) -> None:
    run = _edits_diff if action == "diff" else _edits_restore

    with pytest.raises(ToolError) as exc:
        await run(_StubManager(err), "edits", action, "file-x")

    payload = _payload(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    message = payload["error"]["message"]
    assert message.startswith(f"Cannot {action} 'file-x': its live file")
    assert payload["backup_name"] == "file-x"
    assert payload["path"] == "/backup/x.txt"
    if action == "restore":
        assert payload["data"]["apply_status"] == "not_applied"


async def test_on_demand_capture_of_a_refused_path_reports_the_refusal() -> None:
    with pytest.raises(ToolError) as exc:
        await capture_on_demand(
            _StubManager(_capture_failure()), "file", "/backup/x.txt"
        )

    payload = _payload(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["path"] == "/backup/x.txt"


def test_suggestion_rules_match_the_component_allowlist() -> None:
    # Loaded by file path: importing the package would need the HA stubs.
    const_py = Path(__file__).parents[3] / "custom_components/ha_mcp_tools/const.py"
    spec = importlib.util.spec_from_file_location("_component_const", const_py)
    assert spec is not None and spec.loader is not None
    const = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(const)

    assert auto_backup._VOLUME_ROOTS == const.ALLOWED_VOLUME_ROOTS
    assert auto_backup._DENY_SEGMENTS == const.DENY_PATH_SEGMENTS
    assert auto_backup._DENY_BASENAMES == const.DENY_READ_BASENAMES
