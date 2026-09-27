"""Codex BAT adapter contracts, without a live model or HA container."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from uat import codex_agent  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "run_uat_codex", ROOT / "tests/uat/run_uat.py"
)
run_uat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_uat)


def test_private_profile_only_grants_bat_mcp(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode":"fixture"}')
    monkeypatch.setenv("CODEX_HOME", str(source))
    config = run_uat.build_stdio_mcp_config(
        "http://127.0.0.1:8123", "ha-fixture", None, None
    )
    root, home = codex_agent.prepare_home(config)
    try:
        profile = tomllib.loads((home / "bat.config.toml").read_text())
        server = profile["mcp_servers"]["home-assistant"]
        assert server["command"] == "uv"
        assert server["env"]["HOMEASSISTANT_TOKEN"] == "ha-fixture"
        assert profile["permissions"]["bat"]["network"]["enabled"] is False
        assert profile["permissions"]["bat"]["extends"] == ":read-only"
        assert (home / "auth.json").read_text() == (source / "auth.json").read_text()
        cmd = codex_agent.command("Find lights", "gpt-6-sol", root / "work")
        assert "--disable" in cmd and "shell_tool" in cmd
        assert "ha-fixture" not in " ".join(cmd)
        assert "--strict-config" in cmd and "--ephemeral" in cmd
        assert "--skip-git-repo-check" in cmd
        (home / "auth.json").write_text('{"auth_mode":"rotated"}')
        codex_agent.persist_auth(home)
        assert (source / "auth.json").read_text() == '{"auth_mode":"rotated"}'
    finally:
        import shutil

        shutil.rmtree(root)


def test_jsonl_metrics_are_observed_not_inferred():
    events = [
        {"type": "thread.started", "thread_id": "fixture"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "tool": "ha_search",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "tool": "ha_config_set_automation",
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Found lights"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 30,
            },
        },
    ]
    result = codex_agent.parse_events(
        "\n".join(map(json.dumps, events)), {"exit_code": 0}
    )
    assert result["completed"] is True
    assert result["output"] == "Found lights"
    assert result["tool_stats"] == {"totalCalls": 2, "totalSuccess": 1, "totalFail": 1}
    assert result["tool_sequence"] == ["ha_search", "ha_config_set_automation"]
    assert result["tokens_cached"] == 20
    assert (
        run_uat.make_phase_summary("test", {**result, "duration_ms": 1, "stderr": ""})[
            "tool_sequence"
        ]
        == result["tool_sequence"]
    )


@pytest.mark.parametrize("events", [[], [{"type": "turn.failed"}]])
def test_missing_or_failed_turn_is_not_success(events):
    result = codex_agent.parse_events(
        "\n".join(map(json.dumps, events)), {"exit_code": 0}
    )
    assert result["completed"] is False


def test_missing_auth_fails_before_model(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(ValueError, match=r"CODEX_HOME/auth\.json"):
        codex_agent.prepare_home({"mcpServers": {"home-assistant": {}}})
