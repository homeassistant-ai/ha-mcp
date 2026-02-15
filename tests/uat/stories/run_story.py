#!/usr/bin/env python3
"""
Standalone story runner.

Runs YAML stories against a HA test instance:
- Setup: FastMCP in-memory (sub-second, deterministic)
- Test prompt: AI agent CLI via run_uat.py (gemini/claude)
- Each agent gets a fresh HA container (clean state)

Results are appended to a JSONL file for historical tracking.

Usage:
    # Run a single story
    uv run python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini

    # Run all stories
    uv run python tests/uat/stories/run_story.py --all --agents gemini

    # Run against a specific branch/tag
    uv run python tests/uat/stories/run_story.py --all --agents gemini --branch v6.6.1

    # Use an existing HA instance instead of starting a container
    uv run python tests/uat/stories/run_story.py --all --agents gemini --ha-url http://localhost:8123

    # Keep container alive after run (for verification/debugging)
    uv run python tests/uat/stories/run_story.py --all --agents gemini --keep-container

    # Just print the BAT scenario JSON
    uv run python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CATALOG_DIR = SCRIPT_DIR / "catalog"
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
TESTS_DIR = REPO_ROOT / "tests"
RUN_UAT = SCRIPT_DIR.parent / "run_uat.py"
DEFAULT_RESULTS_FILE = REPO_ROOT / "local" / "uat-results.jsonl"

# Add paths for imports
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

logger = logging.getLogger(__name__)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Story loading
# ---------------------------------------------------------------------------
def load_story(path: Path) -> dict:
    """Load a story YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# HA Container
# ---------------------------------------------------------------------------
def _start_container(*, keep_alive: bool = False) -> dict:
    """Start a HA test container, return {url, token, container, config_dir}.

    Args:
        keep_alive: If True, disable ryuk so the container survives process exit.
    """
    import os
    import shutil
    import tempfile

    import requests
    from testcontainers.core.container import DockerContainer

    from test_constants import TEST_TOKEN

    if keep_alive:
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"

    # renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
    HA_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.1.3"

    # Copy initial_test_state
    config_dir = Path(tempfile.mkdtemp(prefix="ha_story_"))
    initial_state = TESTS_DIR / "initial_test_state"
    shutil.copytree(initial_state, config_dir, dirs_exist_ok=True)
    os.chmod(config_dir, 0o755)
    for item in config_dir.rglob("*"):
        if item.is_file():
            os.chmod(item, 0o644)
        elif item.is_dir():
            os.chmod(item, 0o755)

    container = (
        DockerContainer(HA_IMAGE)
        .with_exposed_ports(8123)
        .with_volume_mapping(str(config_dir), "/config", "rw")
        .with_env("TZ", "UTC")
        .with_kwargs(privileged=True)
    )
    container.start()

    try:
        port = container.get_exposed_port(8123)
        url = f"http://localhost:{port}"
        log(f"HA container started on {url}")

        # Wait for HA to be ready
        time.sleep(5)
        start = time.time()
        while time.time() - start < 120:
            try:
                r = requests.get(
                    f"{url}/api/config",
                    headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                    timeout=5,
                )
                if r.status_code == 200:
                    version = r.json().get("version", "unknown")
                    log(f"HA ready (version {version})")
                    break
            except requests.RequestException:
                pass
            time.sleep(3)
        else:
            raise TimeoutError("HA not ready after 120s")

        time.sleep(10)  # component stabilization
    except Exception:
        container.stop()
        shutil.rmtree(config_dir, ignore_errors=True)
        raise

    return {
        "url": url,
        "token": TEST_TOKEN,
        "container": container,
        "config_dir": config_dir,
    }


def _stop_container(ha: dict) -> None:
    """Stop HA container and clean up."""
    import shutil

    log("Stopping HA container...")
    ha["container"].stop()
    shutil.rmtree(ha["config_dir"], ignore_errors=True)


# ---------------------------------------------------------------------------
# Session file detection
# ---------------------------------------------------------------------------
def _find_latest_session_file(agent: str, after: float) -> str | None:
    """Find the most recent session file for an agent created after a timestamp.

    Args:
        agent: Agent name ("gemini" or "claude").
        after: Only consider files modified after this unix timestamp.

    Gemini: ~/.gemini/tmp/<hash>/chats/session-*.json
    Claude: ~/.claude/projects/<dir>/<session>.jsonl
    """
    home = Path.home()

    if agent == "gemini":
        gemini_tmp = home / ".gemini" / "tmp"
        if not gemini_tmp.exists():
            return None
        session_files = [
            p for p in gemini_tmp.glob("*/chats/session-*.json")
            if p.stat().st_mtime > after
        ]
        if not session_files:
            return None
        return str(max(session_files, key=lambda p: p.stat().st_mtime))

    if agent == "claude":
        claude_projects = home / ".claude" / "projects"
        if not claude_projects.exists():
            return None
        session_files = [
            p for p in claude_projects.glob("*/*.jsonl")
            if p.stat().st_mtime > after
        ]
        if not session_files:
            return None
        return str(max(session_files, key=lambda p: p.stat().st_mtime))

    return None


# ---------------------------------------------------------------------------
# FastMCP in-memory setup
# ---------------------------------------------------------------------------
async def _run_mcp_steps(
    ha_url: str, ha_token: str, steps: list[dict], phase: str
) -> None:
    """Execute setup or teardown steps via FastMCP in-memory client."""
    if not steps:
        return

    import ha_mcp.config

    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer

    ha_mcp.config._settings = None

    client = HomeAssistantClient(base_url=ha_url, token=ha_token)
    server = HomeAssistantSmartMCPServer(client=client)

    from fastmcp import Client

    async with Client(server.mcp) as mcp_client:
        for step in steps:
            tool_name = step["tool"]
            args = step.get("args", {})
            log(f"  [{phase}] {tool_name}({args})")
            try:
                await mcp_client.call_tool(tool_name, args)
            except Exception as e:
                if phase == "setup":
                    log(f"  [{phase}] {tool_name} FAILED: {e}")
                    raise
                else:
                    log(f"  [{phase}] {tool_name} failed (ok): {e}")


# ---------------------------------------------------------------------------
# Test prompt execution via agent CLI
# ---------------------------------------------------------------------------
def _run_test_prompt(
    prompt: str,
    agent: str,
    ha_url: str,
    ha_token: str,
    branch: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, dict | None]:
    """Run test prompt via run_uat.py for a single agent. Returns (exit_code, parsed_summary)."""
    scenario = {"test_prompt": prompt.strip()}

    cmd = [
        sys.executable,
        str(RUN_UAT),
        "--agents", agent,
        "--ha-url", ha_url,
        "--ha-token", ha_token,
    ]
    if branch:
        cmd.extend(["--branch", branch])
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
    )

    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

    summary = None
    if result.stdout.strip():
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    return result.returncode, summary


# ---------------------------------------------------------------------------
# Git info
# ---------------------------------------------------------------------------
def get_git_info() -> tuple[str, str]:
    """Get (short SHA, git describe) for the current commit."""
    sha = "unknown"
    describe = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        sha = result.stdout.strip()
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        describe = result.stdout.strip()
    except Exception:
        pass
    return sha, describe


# ---------------------------------------------------------------------------
# JSONL results
# ---------------------------------------------------------------------------
def append_result(
    results_file: Path,
    story: dict,
    agent: str,
    sha: str,
    describe: str,
    branch: str | None,
    bat_summary: dict,
    session_file: str | None = None,
) -> None:
    """Append a single story result as one JSONL line."""
    agent_data = bat_summary.get("agents", {}).get(agent, {})
    test_phase = agent_data.get("test", {})
    aggregate = agent_data.get("aggregate", {})

    record = {
        "sha": sha,
        "version": describe,
        "branch": branch,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "story": story["id"],
        "category": story["category"],
        "weight": story["weight"],
        "passed": agent_data.get("all_passed", False),
        "test_duration_ms": test_phase.get("duration_ms"),
        "total_duration_ms": aggregate.get("total_duration_ms"),
        "tool_calls": aggregate.get("total_tool_calls"),
        "tool_failures": aggregate.get("total_tool_fail"),
        "turns": test_phase.get("num_turns"),
    }
    if session_file:
        record["session_file"] = session_file

    results_file.parent.mkdir(parents=True, exist_ok=True)
    with open(results_file, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run_stories(args: argparse.Namespace, filtered: list[tuple[Path, dict]]) -> int:
    """Run stories with a fresh container per agent.

    For each agent: start container -> run all stories -> stop container.
    When --ha-url is provided, all agents share the external instance.
    """
    sha, describe = get_git_info()
    agent_list = [a.strip() for a in args.agents.split(",")]
    using_external_ha = bool(args.ha_url)

    all_results: list[tuple[str, str, dict, int, dict | None, str | None]] = []
    # Each entry: (agent, story_id, story, exit_code, summary, session_file)

    for agent in agent_list:
        log(f"\n{'#'*60}")
        log(f"Agent: {agent}")
        log(f"{'#'*60}")

        # Start a fresh container for this agent (or use external HA)
        ha = None
        ha_url = args.ha_url
        ha_token = args.ha_token
        if not using_external_ha:
            ha = _start_container(keep_alive=args.keep_container)
            ha_url = ha["url"]
            ha_token = ha["token"]

        try:
            for _path, story in filtered:
                sid = story["id"]
                log(f"\n{'='*60}")
                log(f"[{agent}] Story {sid}: {story['title']}")
                log(f"{'='*60}")

                setup_steps = story.get("setup") or []

                # Setup via FastMCP in-memory
                if setup_steps:
                    log(f"[{agent}/{sid}] Setup ({len(setup_steps)} steps via FastMCP)...")
                    await _run_mcp_steps(ha_url, ha_token, setup_steps, "setup")

                # Test via agent CLI
                log(f"[{agent}/{sid}] Running test prompt...")
                run_start = time.time()
                rc, summary = _run_test_prompt(
                    story["prompt"],
                    agent,
                    ha_url,
                    ha_token,
                    args.branch,
                    args.extra_args or None,
                )

                # Detect session file created during this run
                session_file = _find_latest_session_file(agent, after=run_start)

                all_results.append((agent, sid, story, rc, summary, session_file))

                # Append JSONL result
                if summary:
                    append_result(
                        args.results_file, story, agent, sha, describe,
                        args.branch, summary, session_file,
                    )

                if session_file:
                    log(f"[{agent}/{sid}] Session file: {session_file}")

        finally:
            if ha:
                if args.keep_container:
                    log(f"\n[{agent}] Container kept alive: {ha['url']}")
                    log(f"[{agent}] Token: {ha['token']}")
                    log(f"[{agent}] Config dir: {ha['config_dir']}")
                    log(f"[{agent}] Stop manually: docker stop <container>")
                else:
                    _stop_container(ha)

    # Summary
    log(f"\n{'='*60}")
    log("Summary")
    log(f"{'='*60}")
    for agent, sid, story, rc, _, session_file in all_results:
        status = "PASS" if rc == 0 else "FAIL"
        session_info = f" (session: {session_file})" if session_file else ""
        log(f"  [{status}] {agent}/{sid}: {story['title']}{session_info}")

    log(f"\nResults appended to {args.results_file}")

    failed = sum(1 for _, _, _, rc, _, _ in all_results if rc != 0)
    total = len(all_results)
    if failed:
        log(f"\n{failed}/{total} story runs failed")
        return 1
    else:
        log(f"\nAll {total} story runs passed")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run user acceptance stories via BAT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("story_file", nargs="?", help="Path to story YAML file")
    parser.add_argument("--all", action="store_true", help="Run all stories in catalog/")
    parser.add_argument("--agents", default="gemini", help="Comma-separated agent list")
    parser.add_argument("--branch", help="Git branch/tag to install ha-mcp from")
    parser.add_argument("--ha-url", help="Use existing HA instance (skip container)")
    parser.add_argument("--ha-token", help="HA long-lived access token")
    parser.add_argument("--dry-run", action="store_true", help="Print BAT scenario JSON")
    parser.add_argument("--min-weight", type=int, default=1, help="Minimum story weight")
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Keep HA container alive after run (for verification/debugging)",
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
        help=f"JSONL file to append results to (default: {DEFAULT_RESULTS_FILE})",
    )
    parser.add_argument("extra_args", nargs="*", help="Extra args passed to run_uat.py")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.all:
        stories = sorted(CATALOG_DIR.glob("s*.yaml"))
    elif args.story_file:
        story_path = Path(args.story_file)
        if not story_path.is_absolute():
            story_path = SCRIPT_DIR / story_path
        stories = [story_path]
    else:
        parser.print_help()
        sys.exit(1)

    # Filter by weight
    filtered = []
    for path in stories:
        story = load_story(path)
        if story.get("weight", 1) >= args.min_weight:
            filtered.append((path, story))

    if args.dry_run:
        for _, story in filtered:
            scenario = {"test_prompt": story["prompt"].strip()}
            print(f"# {story['id']}: {story['title']}")
            if story.get("setup"):
                print(f"# Setup: {len(story['setup'])} steps (FastMCP in-memory)")
            print(json.dumps(scenario, indent=2))
            print()
        return

    exit_code = asyncio.run(run_stories(args, filtered))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
