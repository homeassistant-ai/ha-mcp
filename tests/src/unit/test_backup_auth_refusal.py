"""An auth failure during a mandatory snapshot is reported as what it is.

The snapshot of a file or YAML write goes through the same admin-only
``ha_mcp_tools`` services as the write, so a refused token fails there first.
Retrying the backup cannot help, and the write stays blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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


_FILE = ("file", "www/x.txt")


@pytest.fixture(
    params=[
        (
            HomeAssistantAdminRequiredError("admin-only"),
            "AUTH_INSUFFICIENT_PERMISSIONS",
            _FILE,
        ),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN", _FILE),
        (_not_sent_after_auth_failure(), "AUTH_INVALID_TOKEN", _FILE),
        (
            HomeAssistantAdminRequiredError("admin-only"),
            "AUTH_INSUFFICIENT_PERMISSIONS",
            ("yaml", "configuration.yaml::template"),
        ),
    ],
    ids=["non_admin", "rejected", "rejected_on_retry", "non_admin_yaml"],
)
def refusal(request: pytest.FixtureRequest) -> tuple[Any, str, tuple[str, str]]:
    """A client whose token bootstrap is refused, the code that must surface, the target."""
    error, code, target = request.param
    client: Any = _StubClient()
    client.get_services = AsyncMock(
        return_value=[
            {"domain": "ha_mcp_tools", "services": {"get_caller_token": {}}},
        ]
    )
    client.call_service = AsyncMock(side_effect=error)
    return client, code, target


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _StubSettings:
    settings = _StubSettings(auto_backup_dir=str(tmp_path / "backups"))
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    return settings


def _code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    return json.loads(str(exc.value))["error"]["code"]


def _tool(ran: list[str], client: Any, domain: str) -> Any:
    @with_auto_backup(
        domain=domain,
        id_param="path",
        mandatory=True,
        client=client,
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    return tool


@pytest.mark.usefixtures("settings")
async def test_mandatory_write_reports_the_failure_and_stays_blocked(
    refusal: tuple[Any, str, tuple[str, str]],
) -> None:
    client, expected_code, (domain, entity_id) = refusal
    ran: list[str] = []

    with pytest.raises(ToolError) as exc:
        await _tool(ran, client, domain)(path=entity_id)

    assert _code(exc) == expected_code
    assert exc.value.log_level == TOOL_ERROR_LOG_LEVEL
    assert ran == []


async def test_on_demand_capture_reports_a_missing_tools_entry(
    settings: _StubSettings,
) -> None:
    client: Any = _StubClient()
    client.get_services = AsyncMock(return_value=[])
    mgr = get_backup_manager(client, settings)

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, *_FILE)

    assert _code(exc) == "COMPONENT_NOT_INSTALLED"


async def test_on_demand_capture_reports_the_failure(
    refusal: tuple[Any, str, tuple[str, str]], settings: _StubSettings
) -> None:
    client, expected_code, (domain, entity_id) = refusal
    mgr = get_backup_manager(client, settings)

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, domain, entity_id)

    assert _code(exc) == expected_code
