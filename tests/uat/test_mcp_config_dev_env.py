"""BAT against a running ha-mcp server: --mcp-url and --dev-env."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest
from uat import mcp_config

from ha_mcp._vendor.fastmcp.mcp_config import MCPConfig, RemoteMCPServer

HA = "https://ha.example.trycloudflare.com"
MCP = "https://mcp.example.trycloudflare.com/private_abc"


def _urls_file(tmp_path: Path, mcp: str = MCP) -> Path:
    path = tmp_path / "dev-ha-env-urls.txt"
    path.write_text(f"HA:  {HA}\nMCP: {mcp}\n")
    return path


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "ha_url": None,
        "mcp_url": None,
        "dev_env": None,
        "branch": None,
        "mcp_env": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dev_env_file_sets_the_ha_and_mcp_urls(tmp_path):
    args = _args(dev_env=str(_urls_file(tmp_path)))
    mcp_config.resolve_running_server(args)
    assert (args.ha_url, args.mcp_url) == (HA, MCP)


def test_a_tunnel_that_is_down_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no usable MCP URL"):
        mcp_config.read_dev_env_urls(_urls_file(tmp_path, mcp="down"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"branch": "master"},
        {"mcp_env": ["ENABLE_TOOL_SEARCH=true"]},
        {"ha_url": HA},
    ],
)
def test_options_for_a_started_server_are_refused_with_dev_env(tmp_path, overrides):
    args = _args(dev_env=str(_urls_file(tmp_path)), **overrides)
    with pytest.raises(ValueError):
        mcp_config.resolve_running_server(args)


def test_claude_and_openai_config_reaches_the_running_server():
    config = mcp_config.build_stdio_mcp_config(HA, "token", None, mcp_url=MCP)
    server = MCPConfig.from_dict(config).mcpServers["home-assistant"]
    assert isinstance(server, RemoteMCPServer)
    assert server.url == MCP
    # Claude Code's --mcp-config needs the type to treat the entry as HTTP.
    assert config["mcpServers"]["home-assistant"]["type"] == "http"


def test_gemini_config_reaches_the_running_server(tmp_path):
    mcp_config.write_gemini_mcp_config(HA, "token", None, tmp_path, mcp_url=MCP)
    settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert settings["mcpServers"]["homeassistant"] == {"httpUrl": MCP}


def test_run_story_dev_env_runs_against_the_environment(tmp_path, monkeypatch):
    from test_constants import TEST_TOKEN
    from uat.stories import run_story

    story = next((Path(run_story.__file__).parent / "catalog").glob("t01_*.yaml"))
    captured: dict = {}

    async def _fake_run_stories(args, filtered):
        captured["args"] = args
        return 0

    monkeypatch.setattr(run_story, "run_stories", _fake_run_stories)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_story.py", str(story), "--dev-env", str(_urls_file(tmp_path))],
    )
    with pytest.raises(SystemExit) as exc:
        run_story.main()

    assert exc.value.code == 0
    args = captured["args"]
    assert (args.ha_url, args.mcp_url, args.ha_token) == (HA, MCP, TEST_TOKEN)
    # Each test prompt runs through mcp_config.py, which must reach the same server.
    assert args.extra_args[args.extra_args.index("--mcp-url") + 1] == MCP
