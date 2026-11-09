"""ha_report_issue tells a pip/uvx user on Python 3.13 to move to 3.14 before
the next release drops it (pip and uv would keep them on the old release)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import update_check
from ha_mcp.tools.tools_bug_report import BugReportTools


@pytest.fixture(autouse=True)
def _pip_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(update_check, "_running_in_docker", lambda: False)


def _on_python(monkeypatch: pytest.MonkeyPatch, major: int, minor: int) -> None:
    monkeypatch.setattr(update_check, "_NEXT_PYTHON", (major, minor + 1))


def test_older_python_gets_the_upgrade_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_python(monkeypatch, *sys.version_info[:2])
    hint = update_check.python_upgrade_hint()
    assert hint is not None
    nxt = f"{sys.version_info[0]}.{sys.version_info[1] + 1}"
    assert f"needs Python {nxt}" in hint
    assert f'"--python", "{nxt}"' in hint


def test_current_python_gets_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "_NEXT_PYTHON", sys.version_info[:2])
    assert update_check.python_upgrade_hint() is None


@pytest.mark.parametrize("deployment", ["embedded", "addon", "docker"])
def test_installs_that_bring_their_python_get_none(
    monkeypatch: pytest.MonkeyPatch, deployment: str
) -> None:
    _on_python(monkeypatch, *sys.version_info[:2])
    if deployment == "embedded":
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    elif deployment == "addon":
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    else:
        monkeypatch.setattr(update_check, "_running_in_docker", lambda: True)
    assert update_check.python_upgrade_hint() is None


async def test_report_carries_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_python(monkeypatch, *sys.version_info[:2])
    client = MagicMock()
    client.get_config = AsyncMock(side_effect=Exception("down"))
    client.call_service = AsyncMock(side_effect=Exception("down"))
    result = await BugReportTools(client).ha_report_issue(tool_call_count=1)
    assert result["python_upgrade_hint"] == update_check.python_upgrade_hint()
    assert "python_upgrade_hint" in result["instructions"]
