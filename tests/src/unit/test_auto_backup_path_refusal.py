"""A write to a path the component refuses reports the refusal (#2908).

The mandatory pre-write capture reads the file through the same path gate as
the write, so it fails first. That must surface as the component's path
refusal, not as a retryable ``BACKUP_CAPTURE_FAILED``. Kept out of
``test_backup_manager.py``, which is at its module-size baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.auto_backup import with_auto_backup

from .test_backup_manager import _StubClient, _StubSettings

pytestmark = pytest.mark.asyncio


async def test_refused_path_write_reports_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _StubSettings(auto_backup_dir=str(tmp_path / "backups"))
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    refusal = "Path not allowed. Allowed paths: www/**"

    async def fake_call(_client: Any, service: str, data: dict[str, Any]) -> Any:
        assert service == "read_file"
        return {"success": False, "error": refusal}

    monkeypatch.setattr(
        "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", fake_call
    )
    monkeypatch.setattr(
        "ha_mcp.tools.util_helpers.unwrap_service_response", lambda r: r
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

    error = json.loads(str(exc.value))["error"]
    assert error["code"] != "BACKUP_CAPTURE_FAILED"
    assert error["message"] == refusal
    assert ran == []  # still blocked
