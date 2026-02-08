#!/usr/bin/env python3
"""
BAT Runner - Bot acceptance testing for ha-mcp.

Executes MCP test scenarios on real AI agent CLIs (Claude, Gemini) against a
Home Assistant test instance. The calling agent generates scenarios dynamically
and evaluates results - this script is a dumb executor.

Full results are written to a temp file. Stdout gets a concise summary with
the file path — the calling agent only reads the full file when needed.

Usage:
    echo '{"test_prompt":"Search for light entities."}' | python tests/uat/run_uat.py --agents gemini
    python tests/uat/run_uat.py --scenario-file /tmp/scenario.json --agents claude,gemini
    python tests/uat/run_uat.py --ha-url http://localhost:8123 --ha-token TOKEN --agents gemini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import requests
from testcontainers.core.container import DockerContainer

# Resolve paths relative to repo root
SCRIPT_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCRIPT_DIR.parent
REPO_ROOT = TESTS_DIR.parent

sys.path.insert(0, str(TESTS_DIR))
from test_constants import TEST_TOKEN  # noqa: E402

# renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
HA_IMAGE = "ghcr.io/home-assistant/home-assistant:2025.12.4"

DEFAULT_TIMEOUT = 120
DEFAULT_AGENTS = "claude,gemini"


# ---------------------------------------------------------------------------
# Logging (stderr only - stdout is reserved for JSON output)
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HA Container Management
# ---------------------------------------------------------------------------
def setup_config_directory() -> Path:
    """Copy initial_test_state to a temp dir for the HA container."""
    config_dir = Path(tempfile.mkdtemp(prefix="ha_bat_"))
    initial_state = TESTS_DIR / "initial_test_state"
    if not initial_state.exists():
        raise FileNotFoundError(f"initial_test_state not found at {initial_state}")

    for item in initial_state.iterdir():
        if item.is_file():
            shutil.copy2(item, config_dir)
        elif item.is_dir():
            shutil.copytree(item, config_dir / item.name)

    # Set permissions
    os.chmod(config_dir, 0o755)
    for item in config_dir.rglob("*"):
        if item.is_file():
            os.chmod(item, 0o644)
        elif item.is_dir():
            os.chmod(item, 0o755)

    return config_dir


def wait_for_ha(url: str, token: str, timeout: int = 120) -> None:
    """Poll HA until the API is ready."""
    log(f"Waiting for HA at {url} ...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{url}/api/config", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if r.status_code == 200:
                version = r.json().get("version", "unknown")
                log(f"HA ready (version {version})")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise TimeoutError(f"HA not ready after {timeout}s")


class HAContainer:
    """Context manager for a disposable HA test container."""

    def __init__(self) -> None:
        self.container: DockerContainer | None = None
        self.config_dir: Path | None = None
        self.url: str = ""
        self.token: str = TEST_TOKEN

    def __enter__(self) -> HAContainer:
        self.config_dir = setup_config_directory()
        self.container = (
            DockerContainer(HA_IMAGE)
            .with_exposed_ports(8123)
            .with_volume_mapping(str(self.config_dir), "/config", "rw")
            .with_env("TZ", "UTC")
            .with_kwargs(privileged=True)
        )
        self.container.start()
        try:
            port = self.container.get_exposed_port(8123)
            self.url = f"http://localhost:{port}"
            log(f"HA container started on {self.url}")
            time.sleep(5)  # initial stabilization
            wait_for_ha(self.url, self.token)
            time.sleep(10)  # component stabilization
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self.container:
            log("Stopping HA container...")
            self.container.stop()
        if self.config_dir and self.config_dir.exists():
            shutil.rmtree(self.config_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# MCP Config Generation
# ---------------------------------------------------------------------------
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


def write_claude_mcp_config(ha_url: str, ha_token: str, branch: str | None) -> Path:
    """Write a temporary Claude MCP config JSON file."""
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
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="claude_mcp_", delete=False)
    json.dump(config, f)
    f.close()
    return Path(f.name)


def write_gemini_mcp_config(ha_url: str, ha_token: str, branch: str | None, workdir: Path) -> None:
    """Write .gemini/settings.json in the given workdir."""
    cmd = mcp_server_command(branch)
    gemini_dir = workdir / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
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


# ---------------------------------------------------------------------------
# Agent Execution
# ---------------------------------------------------------------------------
def check_agent_available(name: str) -> bool:
    """Check if an agent CLI is installed."""
    return shutil.which(name) is not None


async def run_cli(cmd: list[str], timeout: int, cwd: Path | None = None) -> dict:
    """Run a CLI command and capture output."""
    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration_ms = int((time.time() - start) * 1000)
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # Try to parse JSON output
        raw_json = None
        try:
            raw_json = json.loads(stdout_text)
        except json.JSONDecodeError:
            pass

        # Extract fields from JSON if available
        output_text = stdout_text
        num_turns = None
        tool_stats = None
        if raw_json and isinstance(raw_json, dict):
            # Claude JSON format
            if "result" in raw_json:
                output_text = raw_json.get("result", stdout_text)
            # Gemini JSON format
            if "response" in raw_json:
                output_text = raw_json.get("response", stdout_text)
            num_turns = raw_json.get("num_turns")
            tool_stats = raw_json.get("tool_stats")
            # Gemini stats
            if "stats" in raw_json and isinstance(raw_json["stats"], dict):
                tool_stats = raw_json["stats"].get("tools")

        result: dict = {
            "completed": proc.returncode == 0,
            "output": output_text,
            "duration_ms": duration_ms,
            "exit_code": proc.returncode,
            "stderr": stderr_text,
        }
        if num_turns is not None:
            result["num_turns"] = num_turns
        if tool_stats is not None:
            result["tool_stats"] = tool_stats
        if raw_json is not None:
            result["raw_json"] = raw_json
        return result
    except TimeoutError:
        # Terminate the orphaned process
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            proc.kill()
        duration_ms = int((time.time() - start) * 1000)
        return {
            "completed": False,
            "output": "",
            "duration_ms": duration_ms,
            "exit_code": -1,
            "stderr": f"Timed out after {timeout}s",
        }


def build_claude_cmd(prompt: str, mcp_config_path: Path) -> list[str]:
    return [
        "claude",
        "-p", prompt,
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools", "mcp__home-assistant",
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
        "--model", "sonnet",
    ]


def build_gemini_cmd(prompt: str) -> list[str]:
    return [
        "gemini",
        "-p", prompt,
        "--approval-mode", "yolo",
        "--allowed-mcp-server-names", "homeassistant",
        "-o", "json",
    ]


async def run_agent_scenario(
    agent_name: str,
    scenario: dict,
    ha_url: str,
    ha_token: str,
    branch: str | None,
    timeout: int,
) -> dict:
    """Run a full scenario (setup/test/teardown) for one agent."""
    results: dict = {"available": True}

    # Prepare MCP config
    claude_config_path: Path | None = None
    gemini_workdir: Path | None = None

    if agent_name == "claude":
        claude_config_path = write_claude_mcp_config(ha_url, ha_token, branch)
    elif agent_name == "gemini":
        gemini_workdir = Path(tempfile.mkdtemp(prefix="gemini_bat_"))
        write_gemini_mcp_config(ha_url, ha_token, branch, gemini_workdir)

    try:
        for phase in ("setup_prompt", "test_prompt", "teardown_prompt"):
            prompt = scenario.get(phase)
            if not prompt:
                continue

            phase_key = phase.replace("_prompt", "")
            log(f"  [{agent_name}] Running {phase_key}...")

            if agent_name == "claude":
                assert claude_config_path is not None
                cmd = build_claude_cmd(prompt, claude_config_path)
                result = await run_cli(cmd, timeout)
            elif agent_name == "gemini":
                cmd = build_gemini_cmd(prompt)
                result = await run_cli(cmd, timeout, cwd=gemini_workdir)
            else:
                result = {
                    "completed": False,
                    "output": f"Unknown agent: {agent_name}",
                    "duration_ms": 0,
                    "exit_code": -1,
                    "stderr": "",
                }

            results[phase_key] = result
            log(f"  [{agent_name}] {phase_key} completed (exit={result['exit_code']}, {result['duration_ms']}ms)")
    finally:
        # Cleanup temp files
        if claude_config_path and claude_config_path.exists():
            claude_config_path.unlink()
        if gemini_workdir and gemini_workdir.exists():
            shutil.rmtree(gemini_workdir, ignore_errors=True)

    return results


# ---------------------------------------------------------------------------
# Summary Generation
# ---------------------------------------------------------------------------
def make_phase_summary(phase_key: str, phase_result: dict) -> dict:
    """Extract concise summary from a phase result (no raw_json, no stderr, no full output)."""
    summary: dict = {
        "completed": phase_result["completed"],
        "duration_ms": phase_result["duration_ms"],
        "exit_code": phase_result["exit_code"],
    }
    if not phase_result["completed"]:
        # Include output and stderr only on failure — the calling agent needs them to diagnose
        summary["output"] = phase_result.get("output", "")
        stderr = phase_result.get("stderr", "")
        if stderr:
            summary["stderr"] = stderr
    if phase_result.get("num_turns") is not None:
        summary["num_turns"] = phase_result["num_turns"]
    if phase_result.get("tool_stats") is not None:
        summary["tool_stats"] = phase_result["tool_stats"]
    return summary


def make_summary(full_results: dict) -> dict:
    """Build a concise summary from full results (for stdout)."""
    summary: dict = {
        "mcp_source": full_results["mcp_source"],
        "branch": full_results.get("branch"),
        "agents": {},
    }
    for agent_name, agent_data in full_results["results"].items():
        if not agent_data.get("available", False):
            summary["agents"][agent_name] = {"available": False}
            continue

        agent_summary: dict = {"available": True, "all_passed": True}
        for phase_key in ("setup", "test", "teardown"):
            if phase_key not in agent_data:
                continue
            phase_summary = make_phase_summary(phase_key, agent_data[phase_key])
            agent_summary[phase_key] = phase_summary
            if not phase_summary["completed"]:
                agent_summary["all_passed"] = False

        summary["agents"][agent_name] = agent_summary

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> dict:
    """Execute the BAT scenario and return results."""
    # Read scenario
    if args.scenario_file:
        scenario = json.loads(Path(args.scenario_file).read_text())
    else:
        scenario = json.loads(sys.stdin.read())

    if "test_prompt" not in scenario:
        raise ValueError("scenario must contain 'test_prompt'")

    # Determine agents
    requested_agents = [a.strip() for a in args.agents.split(",")]
    agents: dict[str, bool] = {}
    for name in requested_agents:
        available = check_agent_available(name)
        agents[name] = available
        if not available:
            log(f"WARNING: {name} CLI not found, skipping")

    active_agents = [name for name, avail in agents.items() if avail]
    if not active_agents:
        raise ValueError("No agents available")

    # Start HA (container or external)
    ha_url = args.ha_url
    ha_token = args.ha_token or TEST_TOKEN
    mcp_source = "branch" if args.branch else "local"

    container: HAContainer | None = None
    if not ha_url:
        container = HAContainer()
        container.__enter__()
        ha_url = container.url
        ha_token = container.token

    try:
        log(f"HA: {ha_url}")
        log(f"MCP source: {mcp_source}" + (f" ({args.branch})" if args.branch else ""))
        log(f"Agents: {', '.join(active_agents)}")

        # Run agents in parallel
        tasks = {
            name: asyncio.create_task(
                run_agent_scenario(name, scenario, ha_url, ha_token, args.branch, args.timeout)
            )
            for name in active_agents
        }
        agent_results = {}
        for name, task in tasks.items():
            agent_results[name] = await task

        # Add unavailable agents
        for name, avail in agents.items():
            if not avail:
                agent_results[name] = {"available": False}

        return {
            "scenario": scenario,
            "ha_url": ha_url,
            "mcp_source": mcp_source,
            "branch": args.branch,
            "results": agent_results,
        }
    finally:
        if container:
            container.__exit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BAT Runner - Execute MCP test scenarios on AI agent CLIs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  echo '{"test_prompt":"Search for light entities."}' | python tests/uat/run_uat.py --agents gemini
  python tests/uat/run_uat.py --scenario-file /tmp/scenario.json --agents claude,gemini
  python tests/uat/run_uat.py --ha-url http://localhost:8123 --ha-token TOKEN --agents gemini
  python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini
        """,
    )
    parser.add_argument(
        "--agents",
        default=DEFAULT_AGENTS,
        help=f"Comma-separated list of agents to run (default: {DEFAULT_AGENTS})",
    )
    parser.add_argument(
        "--scenario-file",
        help="Read scenario from file instead of stdin",
    )
    parser.add_argument(
        "--ha-url",
        help="Use an existing HA instance instead of starting a container",
    )
    parser.add_argument(
        "--ha-token",
        help="HA long-lived access token (default: test token)",
    )
    parser.add_argument(
        "--branch",
        help="Git branch/tag to install ha-mcp from (default: local code)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout per phase in seconds (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()

    try:
        full_results = asyncio.run(run(args))
    except ValueError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    # Write full results to temp file
    results_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="bat_results_", delete=False
    )
    json.dump(full_results, results_file, indent=2)
    results_file.close()

    # Output concise summary + file path to stdout
    summary = make_summary(full_results)
    summary["results_file"] = results_file.name
    json.dump(summary, sys.stdout, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
