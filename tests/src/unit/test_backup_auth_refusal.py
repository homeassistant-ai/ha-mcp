"""An auth failure during a mandatory snapshot is reported as what it is.

The snapshot of a file or YAML write goes through the same admin-only
``ha_mcp_tools`` services as the write, so a refused token fails there first.
Retrying the backup cannot help, and the write stays blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
)
from ha_mcp.errors import TOOL_ERROR_LOG_LEVEL
from ha_mcp.tools.auto_backup import with_auto_backup
from ha_mcp.tools.backup_on_demand import capture_on_demand

from .test_backup_manager import _StubClient, _StubSettings


def _not_sent_after_auth_failure() -> HomeAssistantCommandNotSent:
    """The shape a REST 401 takes once the WebSocket retry is refused as well."""
    error = HomeAssistantCommandNotSent("WebSocket command was not sent")
    error.__cause__ = HomeAssistantAuthError("Invalid token")
    return error


@pytest.fixture(
    params=[
        (
            HomeAssistantAdminRequiredError("admin-only"),
            "AUTH_INSUFFICIENT_PERMISSIONS",
        ),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN"),
        (_not_sent_after_auth_failure(), "AUTH_INVALID_TOKEN"),
    ],
    ids=["non_admin", "rejected", "rejected_on_retry"],
)
def expected_code(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str:
    error, code = request.param

    async def refused(_client: Any, _service: str, _data: dict[str, Any]) -> Any:
        raise error

    monkeypatch.setattr("ha_mcp.tools.tools_filesystem.call_mcp_tools_service", refused)
    return code


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _StubSettings:
    settings = _StubSettings(auto_backup_dir=str(tmp_path / "backups"))
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    return settings


def _code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    return json.loads(str(exc.value))["error"]["code"]


def _tool(ran: list[str]) -> Any:
    @with_auto_backup(
        domain="file",
        id_param="path",
        mandatory=True,
        client=_StubClient(),
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    return tool


@pytest.mark.usefixtures("settings")
async def test_mandatory_write_reports_the_failure_and_stays_blocked(
    expected_code: str,
) -> None:
    ran: list[str] = []

    with pytest.raises(ToolError) as exc:
        await _tool(ran)(path="www/x.txt")

    assert _code(exc) == expected_code
    assert exc.value.log_level == TOOL_ERROR_LOG_LEVEL
    assert ran == []


async def test_on_demand_capture_reports_the_failure(
    expected_code: str, settings: _StubSettings
) -> None:
    mgr = get_backup_manager(_StubClient(), settings)

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, "file", "www/x.txt")

    assert _code(exc) == expected_code
