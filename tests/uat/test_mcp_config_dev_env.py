"""BAT against a running ha-mcp server: --mcp-url and --dev-env."""

from __future__ import annotations

import argparse
import asyncio
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


@pytest.mark.parametrize("mcp", ["down", "https://", "httpdown", "http://:8123"])
def test_a_urls_file_without_a_usable_mcp_url_is_refused(tmp_path, mcp):
    """A tunnel being restarted shows as ``down``; the run must stop with the
    re-download hint, not fail later at the connection."""
    with pytest.raises(ValueError, match="no usable MCP URL"):
        mcp_config.read_dev_env_urls(_urls_file(tmp_path, mcp=mcp))


def test_a_missing_urls_file_is_an_argument_error(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        mcp_config.read_dev_env_urls(tmp_path / "missing.txt")


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
    # The code under test is the environment's: its results must not claim the
    # local checkout's revision.
    assert mcp_config.running_server_revision(args) == ("unknown", "running server")

    # A test prompt runs through run_uat.py, which must reach the same server.
    sent: dict = {}

    def _fake_run(cmd, **kwargs):
        sent["cmd"] = cmd
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_story.subprocess, "run", _fake_run)
    run_story._run_test_prompt(
        "prompt", "claude", HA, TEST_TOKEN, None, args.extra_args
    )
    assert sent["cmd"][sent["cmd"].index("--mcp-url") + 1] == MCP

    # The inline OpenAI agent connects to the same server itself.
    assert _inline_agent_server(args, monkeypatch) == MCP


def _inline_agent_server(args: argparse.Namespace, monkeypatch) -> str:
    """The server URL the inline OpenAI agent's MCP client is built with."""
    import contextlib

    from uat import openai_agent
    from uat.stories import run_story

    from ha_mcp._vendor import fastmcp

    seen: dict = {}

    class _Client:
        def __init__(self, config):
            seen["config"] = config

        async def __aenter__(self):
            raise RuntimeError("not connecting in a unit test")

        async def __aexit__(self, *exc):
            return False

    class _OpenAI:
        async def close(self):
            return None

    async def _warm(**kwargs):
        return _OpenAI(), "model", None

    monkeypatch.setattr(fastmcp, "Client", _Client)
    monkeypatch.setattr(openai_agent, "create_and_warm_openai_client", _warm)
    args.base_url, args.api_key, args.model, args.max_tools = (
        "http://llm/v1",
        "k",
        None,
        None,
    )

    async def _setup():
        async with contextlib.AsyncExitStack() as stack:
            return await run_story._setup_inline_agent(
                "openai", args.ha_url, args.ha_token, args, stack, {}, [], [], "", ""
            )

    assert asyncio.run(_setup()) is None  # the fake client refused to connect
    return seen["config"]["mcpServers"]["home-assistant"]["url"]


def test_claude_agent_gets_no_tools_or_settings_beyond_the_bat_server():
    """A BAT agent must reach only the server under test: Claude's built-in
    tools (shell, files, web) and the machine's settings and CLAUDE.md could
    point it at another Home Assistant."""
    from uat.run_uat import build_claude_cmd

    cmd = build_claude_cmd("prompt", Path("mcp.json"))
    # Claude Code's CLI: an empty --tools disables every built-in tool, and an
    # empty --setting-sources loads no user, project or local settings.
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd


def test_gemini_agent_gets_no_built_in_tools(tmp_path):
    """Gemini CLI registers only the built-in tools ``tools.core`` lists."""
    mcp_config.write_gemini_mcp_config(HA, "token", None, tmp_path, mcp_url=MCP)
    settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert settings["tools"]["core"] == []


def test_run_story_refuses_mcp_url_passed_through_to_run_uat(monkeypatch):
    """``-- --mcp-url`` would reach only the subprocess agents: the inline agent
    and the recorded revision would describe the local server instead."""
    from uat.stories import run_story

    story = next((Path(run_story.__file__).parent / "catalog").glob("t01_*.yaml"))
    monkeypatch.setattr(
        sys, "argv", ["run_story.py", str(story), "--", "--mcp-url", MCP]
    )
    with pytest.raises(SystemExit) as exc:
        run_story.main()
    assert exc.value.code == 2  # argparse's usage error
