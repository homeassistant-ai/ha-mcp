"""Ignored mandatory disable requests stay visible in settings and reports.

Each test writes its own ``tool_config.json`` in the per-test data dir the
unit conftest provides, and runs on the server's real settings object. The
issue report runs on the server's client connected to a real ``hass``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from starlette.requests import Request

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings, get_global_settings
from ha_mcp.settings_ui import (
    MANDATORY_TOOLS,
    apply_tool_visibility,
    build_tools_handlers,
    dump_tool_metadata_cache,
    load_tool_config,
    save_tool_config,
)
from ha_mcp.tools.bug_report_config import collect_config_toggles
from ha_mcp.tools.tools_bug_report import BugReportTools

IGNORED_BACKUP_WARNING = (
    "Ignoring disabled_tools entry 'ha_manage_backup': "
    "this tool is mandatory and remains enabled."
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Mandatory best practices on (strict), nothing pinned by env vars."""
    current = get_global_settings()
    for name, value in {
        "enable_yaml_config_editing": True,
        "enable_mandatory_bps": True,
        "enable_strict_mandatory_bps": True,
        "disabled_tools": "",
        "pinned_tools": "",
        "enable_snapshot_actions": False,
        "backup_read_only": True,
    }.items():
        monkeypatch.setattr(current, name, value)
    return current


def _body(response: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(bytes(response.body))
    return data


async def _get_tools() -> dict[str, Any]:
    """What the settings page's Tools tab loads (the sidecar's handler)."""
    response = await build_tools_handlers(None)["get_tools"](Request({"type": "http"}))
    return _body(response)


@pytest.mark.parametrize("strict", [False, True])
def test_visibility_warns_for_each_ignored_disable(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    strict: bool,
) -> None:
    monkeypatch.setattr(settings, "enable_strict_mandatory_bps", strict)
    names = MANDATORY_TOOLS | {"ha_get_skill_guide", "ha_restart"}
    config: dict[str, Any] = {"tools": dict.fromkeys(names, "disabled")}
    mcp = MagicMock()

    with caplog.at_level(logging.WARNING):
        apply_tool_visibility(mcp, config, settings)

    ignored = MANDATORY_TOOLS | ({"ha_get_skill_guide"} if strict else set())
    assert len(caplog.records) == len(ignored)
    for name in ignored:
        assert any(name in record.message for record in caplog.records)
    assert config["tools"] == dict.fromkeys(names, "disabled")
    mcp.disable.assert_called_once_with(names=names - ignored)
    if strict:
        assert "acknowledgment key only through it" in caplog.text


async def test_tools_api_exposes_warning_and_preserves_requested_state(
    settings: Settings,
) -> None:
    save_tool_config(
        {"tools": {"ha_manage_backup": "disabled", "ha_restart": "disabled"}}
    )
    dump_tool_metadata_cache([{"name": "ha_manage_backup"}])

    data = await _get_tools()

    assert data["states"]["ha_manage_backup"] == "disabled"
    assert data["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert "ha_manage_backup" in data["tool_config_warnings"][0]


@pytest.mark.parametrize("env_disabled", [False, True])
async def test_tools_api_roundtrip_does_not_conflict_or_persist_env_entries(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, env_disabled: bool
) -> None:
    if env_disabled:
        monkeypatch.setattr(settings, "disabled_tools", "ha_manage_backup")
    save_tool_config({"tools": {"ha_manage_backup": "disabled"}})
    dump_tool_metadata_cache([{"name": "ha_manage_backup"}])

    data = await _get_tools()

    assert data["states"]["ha_manage_backup"] == "disabled"
    assert data["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert data["env_pinned"] == (
        {"ha_manage_backup": "disabled"} if env_disabled else {}
    )

    async def receive() -> dict[str, Any]:
        return {
            "type": "http.request",
            "body": json.dumps({"states": data["states"]}).encode(),
        }

    saved = await build_tools_handlers(None)["save_tools"](
        Request({"type": "http", "method": "POST"}, receive)
    )
    assert saved.status_code == 200
    stored = load_tool_config()["tools"]
    assert stored.get("ha_manage_backup") == (None if env_disabled else "disabled")


async def test_tools_api_keeps_conservative_bps_lock_on_settings_lookup_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_tool_config({"tools": {"ha_get_skill_guide": "disabled"}})
    dump_tool_metadata_cache([{"name": "ha_get_skill_guide"}])
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        MagicMock(side_effect=RuntimeError("settings unavailable")),
    )

    data = await _get_tools()

    assert data["bps_locked_tools"] == ["ha_get_skill_guide"]
    assert data["states"]["ha_get_skill_guide"] == "disabled"
    assert data["ignored_disabled_tools"] is None
    assert data["tool_config_warnings"] is None


def test_report_uses_effective_config_counts_and_warns(settings: Settings) -> None:
    save_tool_config(
        {"tools": {"ha_manage_backup": "disabled", "ha_restart": "disabled"}}
    )

    toggles = collect_config_toggles(settings)

    assert toggles["disabled_tools_count"] == 0
    assert toggles["requested_disabled_tools_count"] == 2
    assert toggles["effective_disabled_tools_count"] == 1
    assert toggles["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert toggles["tool_config_warnings"] == [IGNORED_BACKUP_WARNING]
    assert toggles["enable_snapshot_actions"] is False
    assert toggles["backup_read_only"] is True


@pytest.mark.parametrize("raw_config", [[], {"tools": []}, {"tools": None}])
def test_malformed_tool_config_keeps_known_settings_and_reports_unavailable(
    settings: Settings, caplog: pytest.LogCaptureFixture, raw_config: Any
) -> None:
    save_tool_config(raw_config)

    with caplog.at_level(logging.ERROR):
        toggles = collect_config_toggles(settings)

    assert toggles["enable_snapshot_actions"] is False
    assert toggles["backup_read_only"] is True
    assert toggles["ignored_disabled_tools"] is None
    assert toggles["tool_config_warnings"] is None
    assert toggles["requested_disabled_tools_count"] is None
    assert toggles["effective_disabled_tools_count"] is None
    assert "unavailable" in toggles["tool_config_status"]
    assert "tool configuration diagnostics" in caplog.text


@pytest.mark.parametrize("strict", [False, True])
def test_report_conditional_bps_warning(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    monkeypatch.setattr(settings, "enable_strict_mandatory_bps", strict)
    save_tool_config({"tools": {"ha_get_skill_guide": "disabled"}})

    toggles = collect_config_toggles(settings)

    assert toggles["effective_disabled_tools_count"] == (0 if strict else 1)
    assert toggles["ignored_disabled_tools"] == (
        ["ha_get_skill_guide"] if strict else []
    )
    if strict:
        assert (
            "acknowledgment key only through it" in toggles["tool_config_warnings"][0]
        )


@pytest.mark.parametrize("report_type", ["runtime_bug", "agent_behavior"])
@pytest.mark.parametrize("config_available", [False, True])
async def test_report_output_contains_ignored_disable_warning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    settings: Settings,
    report_type: str,
    config_available: bool,
) -> None:
    if config_available:
        save_tool_config({"tools": {"ha_manage_backup": "disabled"}})
    else:
        save_tool_config({"tools": []})

    result = await BugReportTools(core_client).ha_report_issue(report_type=report_type)

    diagnostics = result["diagnostic_info"]
    if not config_available:
        assert diagnostics["ignored_disabled_tools"] is None
        assert diagnostics["tool_config_warnings"] is None
        assert "unavailable" in diagnostics["tool_config_status"]
        assert "unavailable" in result["issue_body"]
        assert "backup_read_only" in result["issue_body"]
        return
    assert diagnostics["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert diagnostics["tool_config_warnings"] == [IGNORED_BACKUP_WARNING]
    assert IGNORED_BACKUP_WARNING in result["issue_body"]
    assert IGNORED_BACKUP_WARNING in result["formatted_report"]
