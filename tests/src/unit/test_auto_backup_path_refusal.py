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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import (
    BackupManager,
    MandatoryBackupError,
    get_backup_manager,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.auto_backup import PathNotAllowedError, with_auto_backup
from ha_mcp.tools.backup_on_demand import capture_on_demand

from ._backup_restore_helpers import backup_tool
from ._tool_errors import error_body

READ_REFUSAL = "Path not allowed. Allowed paths: www/**, blueprints/**"


@pytest.fixture
async def tools_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` with the File & YAML Tools entry, whose read gate refuses."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert entry.state is ConfigEntryState.LOADED
    return core_client


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )


async def test_refused_path_write_reports_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tools_client: HomeAssistantClient,
) -> None:
    """The component's own read gate refuses the path during the capture."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    ran: list[str] = []

    @with_auto_backup(
        domain="file", id_param="path", mandatory=True, client=tools_client
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    with (
        patch.object(
            tools_client, "call_service", wraps=tools_client.call_service
        ) as spy,
        pytest.raises(ToolError) as exc,
    ):
        await tool(path="/backup/x.txt")

    assert any(call.args[:2] == (DOMAIN, "read_file") for call in spy.call_args_list)
    payload = error_body(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["error"]["message"] == "Path not allowed: /backup/x.txt"
    assert payload["path"] == "/backup/x.txt"
    assert payload["component_error"].startswith("Path not allowed")
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


def _manager(err: BaseException) -> BackupManager:
    """A real manager whose snapshot operations fail with ``err``."""
    mgr = BackupManager(SimpleNamespace(), None)
    raising = AsyncMock(side_effect=err)
    for operation in ("diff_snapshot", "restore_snapshot"):
        setattr(mgr, operation, raising)
    return mgr


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
    action: str, err: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = backup_tool(monkeypatch, _manager(err))

    with pytest.raises(ToolError) as exc:
        await tool(scope="edits", action=action, backup_name="file-x")

    payload = error_body(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    message = payload["error"]["message"]
    assert message.startswith(f"Cannot {action} 'file-x': its live file")
    assert payload["backup_name"] == "file-x"
    assert payload["path"] == "/backup/x.txt"
    if action == "restore":
        assert payload["data"]["apply_status"] == "not_applied"


async def test_on_demand_capture_of_a_refused_path_reports_the_refusal(
    tmp_path: Path, tools_client: HomeAssistantClient
) -> None:
    mgr = get_backup_manager(tools_client, _settings(tmp_path))

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, "file", "/backup/x.txt")

    payload = error_body(exc)
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["path"] == "/backup/x.txt"


def test_suggestion_rules_match_the_component_allowlist() -> None:
    # Loaded by file path, without importing the component package.
    const_py = Path(__file__).parents[3] / "custom_components/ha_mcp_tools/const.py"
    spec = importlib.util.spec_from_file_location("_component_const", const_py)
    assert spec is not None and spec.loader is not None
    const = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(const)

    def suggestions(path: str) -> str:
        error = json.loads(str(PathNotAllowedError(path, READ_REFUSAL)))["error"]
        return json.dumps(error)

    roots = ", ".join(const.ALLOWED_VOLUME_ROOTS)
    assert f"only under {roots};" in suggestions("/backup/x.txt")
    for root in const.ALLOWED_VOLUME_ROOTS:
        assert "add it" in suggestions(f"{root}/x.txt")
    for segment in const.DENY_PATH_SEGMENTS:
        assert "never available" in suggestions(f"{segment}/x")
    for basename in const.DENY_READ_BASENAMES:
        assert "never available" in suggestions(f"www/{basename}")
