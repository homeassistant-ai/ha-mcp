"""Ignored mandatory disable requests stay visible in settings and reports."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from ha_mcp.settings_ui import _handlers_tools, _persistence, _tools_meta
from ha_mcp.tools import tools_bug_report


def _settings(*, strict: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enable_yaml_config_editing=True,
        enable_mandatory_bps=True,
        enable_strict_mandatory_bps=strict,
        disabled_tools="",
        pinned_tools="",
        enable_snapshot_actions=False,
        backup_read_only=True,
    )


@pytest.mark.parametrize("strict", [False, True])
def test_visibility_warns_for_each_ignored_disable(caplog, strict: bool) -> None:
    names = _tools_meta.MANDATORY_TOOLS | {"ha_get_skill_guide", "ha_restart"}
    config = {"tools": dict.fromkeys(names, "disabled")}
    mcp = MagicMock()
    with caplog.at_level(logging.WARNING):
        _tools_meta.apply_tool_visibility(mcp, config, _settings(strict=strict))
    ignored = _tools_meta.MANDATORY_TOOLS | (
        {"ha_get_skill_guide"} if strict else set()
    )
    assert len(caplog.records) == len(ignored)
    for name in ignored:
        assert any(name in record.message for record in caplog.records)
    assert config["tools"] == dict.fromkeys(names, "disabled")
    mcp.disable.assert_called_once_with(names=names - ignored)
    if strict:
        assert "acknowledgment key only through it" in caplog.text


@pytest.mark.asyncio
async def test_tools_api_exposes_warning_and_preserves_requested_state(
    monkeypatch,
) -> None:
    config = {"tools": {"ha_manage_backup": "disabled", "ha_restart": "disabled"}}
    monkeypatch.setattr(_persistence, "effective_tool_config", lambda: config)
    monkeypatch.setattr(_persistence, "env_pinned_tools", dict)
    monkeypatch.setattr(
        _persistence, "load_tool_metadata_cache", lambda: [{"name": "ha_manage_backup"}]
    )
    monkeypatch.setattr("ha_mcp.config.get_global_settings", _settings)
    response = await _handlers_tools._get_tools(None, Request({"type": "http"}))
    data = json.loads(response.body)
    assert data["states"]["ha_manage_backup"] == "disabled"
    assert data["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert "ha_manage_backup" in data["tool_config_warnings"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("env_disabled", [False, True])
async def test_tools_api_roundtrip_does_not_conflict_or_persist_env_entries(
    monkeypatch, tmp_path, env_disabled: bool
) -> None:
    settings = _settings()
    settings.disabled_tools = "ha_manage_backup" if env_disabled else ""
    path = tmp_path / "tool_config.json"
    path.write_text(json.dumps({"tools": {"ha_manage_backup": "disabled"}}))
    monkeypatch.setattr(_persistence, "_get_config_path", lambda: path)
    monkeypatch.setattr(_persistence, "get_global_settings", lambda: settings)
    monkeypatch.setattr("ha_mcp.config.get_global_settings", lambda: settings)
    monkeypatch.setattr(
        _persistence,
        "load_tool_metadata_cache",
        lambda: [{"name": "ha_manage_backup"}],
    )
    response = await _handlers_tools._get_tools(None, Request({"type": "http"}))
    data = json.loads(response.body)
    assert data["states"]["ha_manage_backup"] == "disabled"
    assert data["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert data["env_pinned"] == (
        {"ha_manage_backup": "disabled"} if env_disabled else {}
    )

    async def receive() -> dict:
        return {
            "type": "http.request",
            "body": json.dumps({"states": data["states"]}).encode(),
        }

    saved = await _handlers_tools._save_tools(
        None, Request({"type": "http", "method": "POST"}, receive)
    )
    assert saved.status_code == 200
    stored = json.loads(path.read_text())["tools"]
    assert stored.get("ha_manage_backup") == (None if env_disabled else "disabled")


def test_report_uses_effective_config_counts_and_warns(monkeypatch) -> None:
    config = {"tools": {"ha_manage_backup": "disabled", "ha_restart": "disabled"}}
    monkeypatch.setattr(_persistence, "effective_tool_config", lambda settings: config)
    toggles = tools_bug_report._get_config_toggles(_settings())
    assert toggles["disabled_tools_count"] == 0
    assert toggles["requested_disabled_tools_count"] == 2
    assert toggles["effective_disabled_tools_count"] == 1
    assert toggles["ignored_disabled_tools"] == ["ha_manage_backup"]
    assert toggles["enable_snapshot_actions"] is False
    assert toggles["backup_read_only"] is True
    rendered = tools_bug_report._format_config_toggles_for_template(toggles)
    assert "ha_manage_backup" in rendered
    assert "mandatory" in rendered
    assert "remains enabled" in rendered


@pytest.mark.parametrize("raw_config", [[], {"tools": []}, {"tools": None}])
def test_malformed_tool_config_keeps_known_settings_and_reports_unavailable(
    monkeypatch, tmp_path, caplog, raw_config
) -> None:
    path = tmp_path / "tool_config.json"
    path.write_text(json.dumps(raw_config))
    monkeypatch.setattr(_persistence, "_get_config_path", lambda: path)
    with caplog.at_level(logging.ERROR):
        toggles = tools_bug_report._get_config_toggles(_settings())
    assert toggles["enable_snapshot_actions"] is False
    assert toggles["backup_read_only"] is True
    assert toggles["ignored_disabled_tools"] is None
    assert toggles["tool_config_warnings"] is None
    assert toggles["requested_disabled_tools_count"] is None
    assert toggles["effective_disabled_tools_count"] is None
    assert "unavailable" in toggles["tool_config_status"]
    assert "tool configuration diagnostics" in caplog.text
    rendered = tools_bug_report._format_config_toggles_for_template(toggles)
    assert "unavailable" in rendered
    assert "backup_read_only" in rendered


@pytest.mark.asyncio
async def test_tools_api_keeps_conservative_bps_lock_on_settings_lookup_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        _persistence,
        "effective_tool_config",
        lambda: {"tools": {"ha_get_skill_guide": "disabled"}},
    )
    monkeypatch.setattr(_persistence, "env_pinned_tools", dict)
    monkeypatch.setattr(
        _persistence,
        "load_tool_metadata_cache",
        lambda: [{"name": "ha_get_skill_guide"}],
    )
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        MagicMock(side_effect=RuntimeError("settings unavailable")),
    )
    response = await _handlers_tools._get_tools(None, Request({"type": "http"}))
    data = json.loads(response.body)
    assert data["bps_locked_tools"] == ["ha_get_skill_guide"]
    assert data["states"]["ha_get_skill_guide"] == "disabled"
    assert data["ignored_disabled_tools"] is None
    assert data["tool_config_warnings"] is None


@pytest.mark.parametrize("strict", [False, True])
def test_report_conditional_bps_warning(monkeypatch, strict: bool) -> None:
    monkeypatch.setattr(
        _persistence,
        "effective_tool_config",
        lambda settings: {"tools": {"ha_get_skill_guide": "disabled"}},
    )
    toggles = tools_bug_report._get_config_toggles(_settings(strict=strict))
    assert toggles["effective_disabled_tools_count"] == (0 if strict else 1)
    assert toggles["ignored_disabled_tools"] == (
        ["ha_get_skill_guide"] if strict else []
    )
    if strict:
        assert (
            "acknowledgment key only through it" in toggles["tool_config_warnings"][0]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", ["runtime_bug", "agent_behavior"])
@pytest.mark.parametrize("config_available", [False, True])
async def test_report_output_contains_ignored_disable_warning(
    monkeypatch, report_type: str, config_available: bool
) -> None:
    monkeypatch.setattr(
        _persistence,
        "effective_tool_config",
        lambda settings: (
            {"tools": {"ha_manage_backup": "disabled"}}
            if config_available
            else {"tools": []}
        ),
    )
    monkeypatch.setattr(tools_bug_report, "get_global_settings", _settings)
    monkeypatch.setattr(tools_bug_report, "_detect_installation_method", lambda: "pip")
    monkeypatch.setattr(tools_bug_report, "_detect_mcp_transport", lambda: "http")
    monkeypatch.setattr(tools_bug_report, "get_recent_logs", lambda **kwargs: [])
    monkeypatch.setattr(tools_bug_report, "get_startup_logs", list)
    monkeypatch.setattr(
        tools_bug_report, "_fetch_core_error_log", AsyncMock(return_value="")
    )
    report = tools_bug_report.BugReportTools(
        SimpleNamespace(
            get_config=AsyncMock(return_value={"version": "2026.9.4"}),
            get_states=AsyncMock(return_value=[]),
        )
    )
    for name in (
        "_detect_component_version",
        "_detect_tools_entry_status",
        "_detect_server_entry_status",
    ):
        monkeypatch.setattr(report, name, AsyncMock(return_value=None))
    result = await report.ha_report_issue(report_type=report_type)
    diagnostics = result["diagnostic_info"]
    if not config_available:
        assert diagnostics["ignored_disabled_tools"] is None
        assert diagnostics["tool_config_warnings"] is None
        assert "unavailable" in diagnostics["tool_config_status"]
        assert "unavailable" in result["issue_body"]
        assert "backup_read_only" in result["issue_body"]
        return
    assert diagnostics["ignored_disabled_tools"] == ["ha_manage_backup"]
    warning = diagnostics["tool_config_warnings"][0]
    assert warning == (
        "Ignoring disabled_tools entry 'ha_manage_backup': "
        "this tool is mandatory and remains enabled."
    )
    assert warning in result["issue_body"]
    assert warning in result["formatted_report"]
