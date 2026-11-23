"""A path the component refuses is reported as a path refusal (#2908).

The mandatory pre-write capture reads the file through the component's read
gate, which is wider than the write gate, so a refused path fails there first.
That must surface as the path refusal, not as a retryable
``BACKUP_CAPTURE_FAILED``, and without the read allowlist. Diffing, restoring
or re-capturing a snapshot of such a path says the live file is unreadable.
"""

from __future__ import annotations

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

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._backup_core import snapshot_manager as snapshot_manager
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
    ("path", "suggestion"),
    [
        ("pyscript/x.py", "add it"),
        ("/share/notes.md", "add it"),
        ("/backup/x.txt", "only under /share, /media, /ssl;"),
        ("/sharefoo/x.txt", "only under /share, /media, /ssl;"),
        (".storage/auth", "never available"),
        ("pyscript/.storage/x", "never available"),
        ("www/secrets.yaml", "never available"),
        ("www/Secrets.YAML", "never available"),
    ],
)
async def test_the_refusal_says_whether_the_settings_can_allow_the_path(
    tmp_path: Path, tools_client: HomeAssistantClient, path: str, suggestion: str
) -> None:
    """The component tells, with its refusal, whether the path could be added
    under Custom filesystem paths."""
    mgr = get_backup_manager(tools_client, _settings(tmp_path))

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, "file", path)

    assert suggestion in json.dumps(error_body(exc)["error"])


async def test_a_refusal_without_the_verdict_suggests_no_custom_path() -> None:
    """A component that predates the verdict sends only its text, so whether
    the path could be allowed is unknown."""
    error = str(PathNotAllowedError("pyscript/x.py", READ_REFUSAL))

    assert "Custom filesystem paths" not in error
    assert "never available" not in error


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
    action: str,
    err: BaseException,
    core_client: HomeAssistantClient,
    snapshot_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server's own manager fails the snapshot operation with ``err``.
    for operation in ("diff_snapshot", "restore_snapshot"):
        monkeypatch.setattr(snapshot_manager, operation, AsyncMock(side_effect=err))

    with pytest.raises(ToolError) as exc:
        await manage_backup(
            core_client, scope="edits", action=action, backup_name="file-x"
        )

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
