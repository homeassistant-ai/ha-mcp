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

Exit status is part of the contract: 0 means the agent CLI answered, and any
non-zero exit means the query failed and its output must not be scored as a
verification result. The answer text is still printed to stdout either way,
annotated with an ``[exit N]`` marker, because consumers read stdout.
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

# GNU timeout(1)'s convention, so a hang is distinguishable from a CLI error.
TIMEOUT_EXIT = 124


def _annotate_failure(output: str, returncode: int, stderr: str = "") -> str:
    """Mark a failed query's output so it cannot read as a real answer.

    Any non-zero exit is a failed query, stderr or not. The danger is not the
    empty answer — it is the partial one, which reads like a result and gets
    scored as a verification outcome.
    """
    if returncode == 0:
        return output
    output += f"\n[exit {returncode}]"
    if stderr:
        output += f"\n[stderr]: {stderr}"
    return output


def _timed_out(exc: subprocess.TimeoutExpired, timeout: int) -> tuple[str, int]:
    """Render a hung CLI in the same shape as any other failed query."""
    partial = exc.stdout or ""
    if isinstance(partial, bytes):
        # POSIX attaches raw, untranslated bytes even under text=True.
        partial = partial.decode(errors="replace")
    return (
        _annotate_failure(partial, TIMEOUT_EXIT, f"timed out after {timeout}s"),
        TIMEOUT_EXIT,
    )


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
) -> tuple[str, int]:
    """Run a query via Gemini CLI with MCP tools.

    Returns the answer text and the CLI's exit code, so callers can tell a
    failed query apart from a real answer — the text alone cannot, since a
    failing CLI may still have printed a partial one.
    """
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
                "-p",
                query,
                "--approval-mode",
                "yolo",
                "--allowed-mcp-server-names",
                "homeassistant",
            ],
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=timeout,
            env=env,
            check=False,
        )

        output = result.stdout
        # Try to extract text from JSON output
        try:
            data = json.loads(output)
            if isinstance(data, dict) and "response" in data:
                output = data["response"]
        except json.JSONDecodeError:
            # Output wasn't JSON; keep the raw stdout text as-is.
            pass

        return (
            _annotate_failure(output, result.returncode, result.stderr),
            result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc, timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_claude_query(
    query: str,
    ha_url: str,
    ha_token: str,
    branch: str | None = None,
    timeout: int = 120,
) -> tuple[str, int]:
    """Run a query via Claude CLI with MCP tools.

    Returns the answer text and the CLI's exit code, so callers can tell a
    failed query apart from a real answer — the text alone cannot, since a
    failing CLI may still have printed a partial one.
    """
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
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="ha_query_claude_", delete=False
    ) as f:
        json.dump(config, f)
        config_file = Path(f.name)

    try:
        # Strip CLAUDECODE to allow nested sessions
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            [
                "claude",
                "-p",
                query,
                "--mcp-config",
                str(config_file),
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__home-assistant",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "bypassPermissions",
                "--model",
                "sonnet",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )

        return (
            _annotate_failure(result.stdout, result.returncode, result.stderr),
            result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc, timeout)
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
        response, returncode = run_gemini_query(
            args.query, args.ha_url, args.ha_token, args.branch, args.timeout
        )
    else:
        response, returncode = run_claude_query(
            args.query, args.ha_url, args.ha_token, args.branch, args.timeout
        )

    # Print the answer first either way — consumers read stdout — then exit
    # non-zero so a failed query can't be scored as a verification result.
    print(response)
    if returncode != 0:
        print(
            f"Error: {args.agent} CLI exited with code {returncode}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
