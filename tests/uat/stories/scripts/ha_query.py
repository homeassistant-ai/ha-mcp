#!/usr/bin/env python3
"""
Query a live HA instance via an AI agent with MCP tools.

Uses the full MCP toolset (WebSocket APIs, config entries, search, etc.)
rather than just REST API calls. This is more powerful for verification
because it can check things like automation configs, script traces, etc.

Usage:
    uv run python tests/uat/stories/scripts/ha_query.py \
      --ha-url http://localhost:PORT --ha-token TOKEN \
      --agent gemini \
      "Does automation.sunset_porch_light exist? Show its triggers and actions."

    # With custom branch
    uv run python tests/uat/stories/scripts/ha_query.py \
      --ha-url http://localhost:PORT --ha-token TOKEN \
      --agent gemini --branch v6.6.1 \
      "List all automations and their states."
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent


def mcp_server_command(branch: str | None) -> list[str]:
    """Build the MCP server command for stdio mode."""
    if branch:
        return [
            "uvx",
            "--from",
            f"git+https://github.com/homeassistant-ai/ha-mcp.git@{branch}",
            "ha-mcp",
        ]
    return ["uv", "run", "--project", str(REPO_ROOT), "ha-mcp"]


def run_gemini_query(
    query: str,
    ha_url: str,
    ha_token: str,
    branch: str | None = None,
    timeout: int = 120,
) -> str:
    """Run a query via Gemini CLI with MCP tools."""
    workdir = Path(tempfile.mkdtemp(prefix="ha_query_gemini_"))
    try:
        cmd = mcp_server_command(branch)
        gemini_dir = workdir / ".gemini"
        gemini_dir.mkdir()
        config = {
            "mcpServers": {
                "homeassistant": {
                    "command": cmd[0],
                    "args": cmd[1:],
                    "env": {
                        "HOMEASSISTANT_URL": ha_url,
                        "HOMEASSISTANT_TOKEN": ha_token,
                    },
                }
            }
        }
        (gemini_dir / "settings.json").write_text(json.dumps(config))

        # Strip CLAUDECODE to allow nested sessions
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            [
                "gemini",
                "-p", query,
                "--approval-mode", "yolo",
                "--allowed-mcp-server-names", "homeassistant",
            ],
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=timeout,
            env=env,
        )

        output = result.stdout
        # Try to extract text from JSON output
        try:
            data = json.loads(output)
            if isinstance(data, dict) and "response" in data:
                output = data["response"]
        except json.JSONDecodeError:
            pass

        if result.returncode != 0 and result.stderr:
            output += f"\n[stderr]: {result.stderr}"

        return output
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_claude_query(
    query: str,
    ha_url: str,
    ha_token: str,
    branch: str | None = None,
    timeout: int = 120,
) -> str:
    """Run a query via Claude CLI with MCP tools."""
    cmd = mcp_server_command(branch)
    config = {
        "mcpServers": {
            "home-assistant": {
                "command": cmd[0],
                "args": cmd[1:],
                "env": {
                    "HOMEASSISTANT_URL": ha_url,
                    "HOMEASSISTANT_TOKEN": ha_token,
                },
            }
        }
    }
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="ha_query_claude_", delete=False
    )
    json.dump(config, f)
    f.close()
    config_file = Path(f.name)

    try:
        # Strip CLAUDECODE to allow nested sessions
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            [
                "claude",
                "-p", query,
                "--mcp-config", str(config_file),
                "--strict-mcp-config",
                "--allowedTools", "mcp__home-assistant",
                "--output-format", "text",
                "--no-session-persistence",
                "--permission-mode", "bypassPermissions",
                "--model", "sonnet",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += f"\n[stderr]: {result.stderr}"

        return output
    finally:
        config_file.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query HA via AI agent with MCP tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", help="The question to ask about the HA instance")
    parser.add_argument("--ha-url", required=True, help="HA instance URL")
    parser.add_argument("--ha-token", required=True, help="HA long-lived access token")
    parser.add_argument(
        "--agent",
        default="gemini",
        choices=["gemini", "claude"],
        help="Agent CLI to use (default: gemini)",
    )
    parser.add_argument("--branch", help="Git branch/tag to install ha-mcp from")
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds (default: 120)",
    )
    args = parser.parse_args()

    if not shutil.which(args.agent):
        print(f"Error: {args.agent} CLI not found", file=sys.stderr)
        sys.exit(1)

    if args.agent == "gemini":
        response = run_gemini_query(
            args.query, args.ha_url, args.ha_token, args.branch, args.timeout
        )
    else:
        response = run_claude_query(
            args.query, args.ha_url, args.ha_token, args.branch, args.timeout
        )

    print(response)


if __name__ == "__main__":
    main()
