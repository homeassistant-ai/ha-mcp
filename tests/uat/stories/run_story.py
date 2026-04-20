#!/usr/bin/env python3
"""
Standalone story runner.

Runs YAML stories against a HA test instance:
- Setup: FastMCP in-memory (sub-second, deterministic)
- Test prompt: AI agent CLI via run_uat.py (gemini/claude/openai)
- Each agent gets a fresh HA container (clean state)

Results are appended to a JSONL file for historical tracking.

Usage:
    # Run a single story
    uv run python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini

    # Run all stories
    uv run python tests/uat/stories/run_story.py --all --agents gemini

    # Run with a local OpenAI-compatible LLM (LM Studio, Ollama, etc.)
    uv run python tests/uat/stories/run_story.py --all --agents openai --base-url http://localhost:1234/v1

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
import contextlib
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    import openai
    from fastmcp import Client as MCPClient

SCRIPT_DIR = Path(__file__).resolve().parent
CATALOG_DIR = SCRIPT_DIR / "catalog"
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
TESTS_DIR = REPO_ROOT / "tests"
RUN_UAT = SCRIPT_DIR.parent / "run_uat.py"
DEFAULT_RESULTS_FILE = REPO_ROOT / "local" / "uat-results.jsonl"

# Agents that can run in-process with a persistent MCP+OpenAI client across
# stories. Claude and Gemini are external CLIs and take the subprocess path.
INLINE_AGENTS: frozenset[str] = frozenset({"openai"})

# Add paths for imports
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(SCRIPT_DIR))  # for scripts/ subdirectory imports

from scripts.verify_story import verify_ha_checks  # noqa: E402
from uat.ha_wait import wait_for_ha_ready  # noqa: E402

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

    from test_constants import HA_TEST_IMAGE, TEST_TOKEN
    from testcontainers.core.container import DockerContainer

    if keep_alive:
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"

    HA_IMAGE = HA_TEST_IMAGE

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

        # Wait for HA to be fully ready (API + components + entities)
        wait_for_ha_ready(url, TEST_TOKEN, log=log)
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
# Token extraction from session files
# ---------------------------------------------------------------------------
def _extract_tokens(session_file: str | None, agent: str) -> dict | None:
    """Extract token usage from an agent session file.

    Returns dict with keys: input, output, cached, thoughts (all ints).
    Only non-cached tokens matter for cost — cached tokens are free.
    """
    if not session_file or not Path(session_file).exists():
        return None

    try:
        if agent == "gemini":
            data = json.loads(Path(session_file).read_text())
            totals = {"input": 0, "output": 0, "cached": 0, "thoughts": 0}
            for msg in data.get("messages", []):
                t = msg.get("tokens", {})
                totals["input"] += t.get("input", 0)
                totals["output"] += t.get("output", 0)
                totals["cached"] += t.get("cached", 0)
                totals["thoughts"] += t.get("thoughts", 0)
            return totals

        if agent == "claude":
            totals = {"input": 0, "output": 0, "cached": 0, "thoughts": 0}
            for line in Path(session_file).read_text().splitlines():
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    totals["input"] += usage.get("input_tokens", 0)
                    totals["output"] += usage.get("output_tokens", 0)
                    totals["cached"] += usage.get(
                        "cache_read_input_tokens", 0
                    ) + usage.get("cache_creation_input_tokens", 0)
            return totals
    except Exception as exc:
        log(f"  Token extraction failed: {exc}")
        return None

    return None


def _extract_tool_calls(session_file: str | None, agent: str) -> int | None:
    """Count tool calls from an agent session file."""
    if not session_file or not Path(session_file).exists():
        return None

    try:
        if agent == "gemini":
            data = json.loads(Path(session_file).read_text())
            count = 0
            for msg in data.get("messages", []):
                count += len(msg.get("toolCalls", []))
            return count

        if agent == "claude":
            count = 0
            for line in Path(session_file).read_text().splitlines():
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            count += 1
            return count
    except Exception as exc:
        log(f"  Tool call extraction failed: {exc}")
        return None

    return None


# ---------------------------------------------------------------------------
# Session file detection
# ---------------------------------------------------------------------------
def _find_session_file_by_id(session_id: str) -> str | None:
    """Find a Claude session file by its session_id (UUID).

    Claude stores sessions at ~/.claude/projects/<dir>/<session_id>.jsonl
    """
    home = Path.home()
    claude_projects = home / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    matches = list(claude_projects.glob(f"*/{session_id}.jsonl"))
    return str(matches[0]) if matches else None


def _find_latest_session_file(agent: str, after: float) -> str | None:
    """Find the most recent session file for an agent created after a timestamp.

    Args:
        agent: Agent name ("gemini" or "claude").
        after: Only consider files modified after this unix timestamp.

    Gemini: ~/.gemini/tmp/<hash>/chats/session-*.json
    Claude: Use session_id from JSON output instead (see _find_session_file_by_id).
    """
    home = Path.home()

    if agent == "gemini":
        gemini_tmp = home / ".gemini" / "tmp"
        if not gemini_tmp.exists():
            return None
        session_files = [
            p
            for p in gemini_tmp.glob("*/chats/session-*.json")
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

    import os

    import ha_mcp.config
    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.client.websocket_client import websocket_manager
    from ha_mcp.server import HomeAssistantSmartMCPServer

    # Point global settings at the test HA instance before resetting.
    # The WebSocket client uses get_global_settings() (reads env vars), not the
    # HomeAssistantClient base_url, so we must set env vars explicitly.
    os.environ["HOMEASSISTANT_URL"] = ha_url
    os.environ["HOMEASSISTANT_TOKEN"] = ha_token
    ha_mcp.config._settings = None

    # Disconnect any cached WebSocket so it reconnects to the test instance.
    await websocket_manager.disconnect()

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
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tools: int | None = None,
    no_think: bool = False,
    max_tokens: int | None = None,
    mcp_env: list[str] | None = None,
) -> tuple[int, dict | None]:
    """Run test prompt via run_uat.py for a single agent. Returns (exit_code, parsed_summary)."""
    scenario = {"test_prompt": prompt.strip()}

    cmd = [
        sys.executable,
        str(RUN_UAT),
        "--agents",
        agent,
        "--ha-url",
        ha_url,
        "--ha-token",
        ha_token,
    ]
    if branch:
        cmd.extend(["--branch", branch])
    if model:
        cmd.extend(["--model", model])
    if base_url:
        cmd.extend(["--base-url", base_url])
    if api_key:
        cmd.extend(["--api-key", api_key])
    if max_tools is not None:
        cmd.extend(["--max-tools", str(max_tools)])
    if no_think:
        cmd.append("--no-think")
    if max_tokens is not None:
        cmd.extend(["--max-tokens", str(max_tokens)])
    if mcp_env:
        for pair in mcp_env:
            cmd.extend(["--mcp-env", pair])
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        timeout=600,
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


async def _run_test_prompt_inline(
    prompt: str,
    agent_name: str,
    openai_client: openai.AsyncOpenAI,
    mcp_client: MCPClient,
    model: str,
    openai_tools: list[dict],
    *,
    no_think: bool = False,
    max_tokens: int | None = None,
) -> tuple[int, dict]:
    """Run the test prompt via the openai_agent library directly.

    Reuses a persistent ``mcp_client``, ``openai_client``, and pre-fetched
    ``openai_tools`` across stories, so no subprocess spawn, MCP server
    restart, model warmup, or ``list_tools`` round trip happens per
    story. Returns ``(exit_code, summary)`` matching the shape of
    ``_run_test_prompt`` so the surrounding story-loop code is agnostic
    to how the test was executed. A failure always produces a summary
    (never ``None``) so the caller can append a row to the results file.
    """
    import traceback

    from uat.openai_agent import DEFAULT_MAX_TOKENS, run_scenario_inline

    tool_trace: list[str] = []
    start = time.time()
    try:
        result = await run_scenario_inline(
            openai_client,
            mcp_client,
            model,
            prompt.strip(),
            max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
            no_think=no_think,
            tool_trace_sink=tool_trace,
            openai_tools=openai_tools,
        )
    except Exception as e:
        log(f"  [{agent_name}] inline run failed ({type(e).__name__}): {e}")
        tb = traceback.format_exc()
        log(tb)
        duration_ms = int((time.time() - start) * 1000)
        return 1, _inline_failure_summary(
            agent_name,
            error_msg=f"{type(e).__name__}: {e}",
            traceback_text=tb,
            duration_ms=duration_ms,
            tool_trace=tool_trace,
        )

    duration_ms = int((time.time() - start) * 1000)
    exit_code = 1 if result.get("hit_iteration_limit") else 0

    # Match the summary shape produced by run_uat.make_summary.
    test_phase = {
        "completed": True,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "output": result.get("result", ""),
        "num_turns": result.get("num_turns"),
        "tool_stats": result.get("tool_stats"),
        "tokens_input": result.get("tokens_input"),
        "tokens_output": result.get("tokens_output"),
        "tool_trace": tool_trace,
        "cost_usd": result.get("cost_usd", 0),
    }
    if result.get("hit_iteration_limit"):
        test_phase["error"] = "hit_iteration_limit"
    tool_stats = result.get("tool_stats") or {}
    aggregate = {
        "total_duration_ms": duration_ms,
        "total_turns": result.get("num_turns"),
        "total_tool_calls": tool_stats.get("totalCalls"),
        "total_tool_success": tool_stats.get("totalSuccess"),
        "total_tool_fail": tool_stats.get("totalFail"),
        "tokens_first_input": result.get("tokens_first_input"),
    }
    summary = {
        "agents": {
            agent_name: {
                "available": True,
                "all_passed": exit_code == 0,
                "test": test_phase,
                "aggregate": aggregate,
            }
        }
    }
    return exit_code, summary


def _inline_failure_summary(
    agent_name: str,
    *,
    error_msg: str,
    traceback_text: str | None = None,
    duration_ms: int = 0,
    tool_trace: list[str] | None = None,
) -> dict:
    """Build a summary dict for a failed inline run (matches make_summary shape)."""
    test_phase: dict = {
        "completed": False,
        "duration_ms": duration_ms,
        "exit_code": 1,
        "output": traceback_text or error_msg,
        "error": error_msg,
        "tool_trace": tool_trace or [],
    }
    return {
        "agents": {
            agent_name: {
                "available": True,
                "all_passed": False,
                "test": test_phase,
                "aggregate": {"total_duration_ms": duration_ms},
            }
        }
    }


def _record_setup_failure(
    filtered: list,
    agent: str,
    error_msg: str,
    *,
    all_results: list,
    results_file,
    sha: str,
    describe: str,
    branch: str | None,
) -> None:
    """Write a failure row per story and record it in all_results.

    Called when per-agent setup (OpenAI client, MCP server) fails before
    any story runs — otherwise every filtered story is silently dropped
    from both the JSONL log and the process exit code.
    """
    for _path, story in filtered:
        summary = _inline_failure_summary(agent, error_msg=error_msg)
        all_results.append((agent, story["id"], story, 1, summary, None))
        append_result(
            results_file,
            story,
            agent,
            sha,
            describe,
            branch,
            summary,
            None,
            exit_code=1,
            verify_results=None,
        )


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
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        sha = result.stdout.strip()
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        describe = result.stdout.strip()
    except Exception:
        pass
    return sha, describe


# ---------------------------------------------------------------------------
# Pass/fail determination
# ---------------------------------------------------------------------------
def _compute_passed(
    exit_code: int,
    tool_calls: int | None,
    verify_results: list[dict] | None,
) -> bool:
    """Determine passed based on tool usage and verification results.

    Rules:
    - Zero tool calls → fail (agent did nothing useful)
    - ha_checks present → all checks must pass
    - No checks → fall back to exit code
    """
    if tool_calls == 0:
        return False
    if verify_results is not None:
        return all(r["passed"] for r in verify_results)
    return exit_code == 0


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
    exit_code: int = 0,
    verify_results: list[dict] | None = None,
) -> None:
    """Append a single story result as one JSONL line."""
    agent_data = bat_summary.get("agents", {}).get(agent, {})
    test_phase = agent_data.get("test", {})
    aggregate = agent_data.get("aggregate", {})

    record = {
        "sha": sha,
        "version": describe,
        "branch": branch,
        "timestamp": datetime.now(UTC).isoformat(),
        "agent": agent,
        "story": story["id"],
        "category": story["category"],
        "weight": story["weight"],
        "passed": _compute_passed(
            exit_code=exit_code,
            tool_calls=aggregate.get("total_tool_calls"),
            verify_results=verify_results,
        ),
        "test_duration_ms": test_phase.get("duration_ms"),
        "total_duration_ms": aggregate.get("total_duration_ms"),
        "tool_calls": aggregate.get("total_tool_calls"),
        "tool_failures": aggregate.get("total_tool_fail"),
        "turns": test_phase.get("num_turns"),
    }
    if session_file:
        record["session_file"] = session_file

    # Cost from Claude's JSON output (direct, no session file needed)
    cost_usd = test_phase.get("cost_usd")
    if cost_usd is not None:
        record["cost_usd"] = cost_usd

    # Extract tool call count from session file if not in summary
    if record["tool_calls"] is None:
        record["tool_calls"] = _extract_tool_calls(session_file, agent)

    # Extract token usage: from phase summary (openai) or session file (claude/gemini)
    tokens = _extract_tokens(session_file, agent)
    if tokens is None:
        ti = test_phase.get("tokens_input")
        to = test_phase.get("tokens_output")
        if ti is not None or to is not None:
            tokens = {"input": ti or 0, "output": to or 0, "cached": 0, "thoughts": 0}
    if tokens:
        record["tokens_input"] = tokens["input"]
        record["tokens_output"] = tokens["output"]
        record["tokens_cached"] = tokens["cached"]
        record["tokens_thoughts"] = tokens["thoughts"]
        record["tokens_billable"] = (tokens["input"] - tokens["cached"]) + tokens["output"]

    # First-turn input tokens: most direct measure of idle context size (openai agent only).
    # Comes from aggregate (not test_phase) since make_phase_summary doesn't propagate it.
    tokens_first_input = aggregate.get("tokens_first_input")
    if tokens_first_input is not None:
        record["tokens_first_input"] = tokens_first_input

    # Include verify_results, agent response, and tool trace only on failure
    if verify_results is not None and not all(r["passed"] for r in verify_results):
        record["verify_results"] = verify_results
        agent_response = test_phase.get("output", "")
        if agent_response:
            record["agent_response"] = agent_response
        tool_trace = test_phase.get("tool_trace")
        if tool_trace:
            record["tool_trace"] = tool_trace

    results_file.parent.mkdir(parents=True, exist_ok=True)
    with open(results_file, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run_stories(
    args: argparse.Namespace, filtered: list[tuple[Path, dict]]
) -> int:
    """Run stories with a fresh container per agent.

    For each agent: start container -> run all stories -> stop container.
    When --ha-url is provided, all agents share the external instance.
    """
    sha, describe = get_git_info()
    agent_list = [a.strip() for a in args.agents.split(",")]
    using_external_ha = bool(args.ha_url)

    from uat.run_uat import (
        _preflight_check_base_url,
        _preflight_check_docker,
        build_stdio_mcp_config,
        parse_mcp_env,
    )

    if not using_external_ha:
        err = _preflight_check_docker()
        if err:
            log(f"FATAL: {err}")
            return 2
    if args.base_url and "openai" in agent_list:
        err = _preflight_check_base_url(args.base_url)
        if err:
            log(f"FATAL: {err}")
            return 2

    all_results: list[tuple[str, str, dict, int, dict | None, str | None]] = []
    # Each entry: (agent, story_id, story, exit_code, summary, session_file)

    mcp_env_dict = parse_mcp_env(
        getattr(args, "mcp_env", None),
        base_url=args.base_url,
        on_default_applied=log,
    )
    effective_mcp_env: list[str] = [f"{k}={v}" for k, v in mcp_env_dict.items()]

    for agent in agent_list:
        log(f"\n{'#' * 60}")
        log(f"Agent: {agent}")
        log(f"{'#' * 60}")

        ha = None
        ha_url = args.ha_url
        ha_token = args.ha_token
        if not using_external_ha:
            ha = _start_container(keep_alive=args.keep_container)
            ha_url = ha["url"]
            ha_token = ha["token"]

        # Inline agents (see INLINE_AGENTS) hold one MCP server + one OpenAI
        # client across all stories for this agent, skipping the per-story
        # MCP server spawn and model warmup. Other agents take the subprocess
        # path via _run_test_prompt.
        use_inline = agent in INLINE_AGENTS
        inline_mcp_client: MCPClient | None = None
        openai_client: openai.AsyncOpenAI | None = None
        resolved_model: str | None = None
        openai_tools: list[dict] = []

        async with contextlib.AsyncExitStack() as agent_stack:
            if ha and not args.keep_container:
                agent_stack.callback(_stop_container, ha)

            if use_inline:
                from fastmcp import Client as _MCPClient
                from uat.openai_agent import (
                    create_and_warm_openai_client,
                    fetch_openai_tools,
                )

                config = build_stdio_mcp_config(
                    ha_url, ha_token, args.branch, mcp_env_dict or None
                )
                try:
                    openai_client, resolved_model = await create_and_warm_openai_client(
                        base_url=args.base_url,
                        api_key=args.api_key,
                        model=args.model,
                    )
                except Exception as e:
                    error_msg = f"Failed to initialise OpenAI client: {type(e).__name__}: {e}"
                    log(f"[{agent}] {error_msg}")
                    _record_setup_failure(
                        filtered,
                        agent,
                        error_msg,
                        all_results=all_results,
                        results_file=args.results_file,
                        sha=sha,
                        describe=describe,
                        branch=args.branch,
                    )
                    continue
                try:
                    inline_mcp_client = await agent_stack.enter_async_context(
                        _MCPClient(config)
                    )
                    openai_tools = await fetch_openai_tools(
                        inline_mcp_client, max_tools=args.max_tools
                    )
                    log(f"[{agent}] MCP server ready ({len(openai_tools)} tools)")
                except Exception as e:
                    error_msg = f"Failed to start MCP server: {type(e).__name__}: {e}"
                    log(f"[{agent}] {error_msg}")
                    _record_setup_failure(
                        filtered,
                        agent,
                        error_msg,
                        all_results=all_results,
                        results_file=args.results_file,
                        sha=sha,
                        describe=describe,
                        branch=args.branch,
                    )
                    continue

            if ha and args.keep_container:
                log(f"\n[{agent}] Container kept alive: {ha['url']}")
                log(f"[{agent}] Token: {ha['token']}")
                log(f"[{agent}] Config dir: {ha['config_dir']}")
                log(f"[{agent}] Stop manually: docker stop <container>")

            for _path, story in filtered:
                sid = story["id"]
                log(f"\n{'=' * 60}")
                log(f"[{agent}] Story {sid}: {story['title']}")
                log(f"{'=' * 60}")

                setup_steps = story.get("setup") or []
                if setup_steps:
                    log(
                        f"[{agent}/{sid}] Setup ({len(setup_steps)} steps via FastMCP)..."
                    )
                    await _run_mcp_steps(ha_url, ha_token, setup_steps, "setup")

                log(f"[{agent}/{sid}] Running test prompt...")
                run_start = time.time()
                summary: dict | None
                if use_inline:
                    assert (
                        openai_client is not None
                        and inline_mcp_client is not None
                        and resolved_model is not None
                    ), "inline setup invariant: clients/model are populated when use_inline is True"
                    rc, summary = await _run_test_prompt_inline(
                        story["prompt"],
                        agent_name=agent,
                        openai_client=openai_client,
                        mcp_client=inline_mcp_client,
                        model=resolved_model,
                        openai_tools=openai_tools,
                        no_think=args.no_think,
                        max_tokens=getattr(args, "max_tokens", None),
                    )
                else:
                    rc, summary = _run_test_prompt(
                        story["prompt"],
                        agent,
                        ha_url,
                        ha_token,
                        args.branch,
                        args.extra_args or None,
                        model=args.model,
                        base_url=args.base_url,
                        api_key=args.api_key,
                        max_tools=args.max_tools,
                        no_think=args.no_think,
                        max_tokens=getattr(args, "max_tokens", None),
                        mcp_env=effective_mcp_env or None,
                    )

                session_file = None
                test_phase = (
                    (summary or {}).get("agents", {}).get(agent, {}).get("test", {})
                )
                claude_session_id = test_phase.get("session_id")
                if claude_session_id:
                    session_file = _find_session_file_by_id(claude_session_id)
                if not session_file:
                    session_file = _find_latest_session_file(agent, after=run_start)

                verify_results = None
                ha_checks = (story.get("verify") or {}).get("ha_checks")
                if ha_checks:
                    agent_output = (
                        (summary or {})
                        .get("agents", {})
                        .get(agent, {})
                        .get("test", {})
                        .get("output", "")
                    )
                    log(f"[{agent}/{sid}] Verifying {len(ha_checks)} ha_check(s)...")
                    verify_results = await verify_ha_checks(
                        ha_url, ha_token, ha_checks, agent_output
                    )
                    failed_checks = [r for r in verify_results if not r["passed"]]
                    if failed_checks:
                        log(f"[{agent}/{sid}] {len(failed_checks)}/{len(ha_checks)} check(s) FAILED")
                        for r in failed_checks:
                            log(f"  FAIL [{r['type']}] {r['detail']}")
                    else:
                        log(f"[{agent}/{sid}] All checks passed")

                all_results.append((agent, sid, story, rc, summary, session_file))

                if summary or verify_results is not None:
                    append_result(
                        args.results_file,
                        story,
                        agent,
                        sha,
                        describe,
                        args.branch,
                        summary or {},
                        session_file,
                        exit_code=rc,
                        verify_results=verify_results,
                    )

                if session_file:
                    log(f"[{agent}/{sid}] Session file: {session_file}")

    # Summary
    log(f"\n{'=' * 60}")
    log("Summary")
    log(f"{'=' * 60}")
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
    parser.add_argument(
        "--all", action="store_true", help="Run all stories in catalog/"
    )
    parser.add_argument("--agents", default="gemini", help="Comma-separated agent list")
    parser.add_argument("--branch", help="Git branch/tag to install ha-mcp from")
    parser.add_argument("--ha-url", help="Use existing HA instance (skip container)")
    parser.add_argument("--ha-token", help="HA long-lived access token")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print BAT scenario JSON"
    )
    parser.add_argument(
        "--min-weight", type=int, default=1, help="Minimum story weight"
    )
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
    parser.add_argument(
        "--model",
        help="Model name (e.g., haiku, sonnet for Claude; auto-detected for openai)",
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible API base URL (required for openai agent)",
    )
    parser.add_argument(
        "--api-key",
        default="no-key",
        help="API key for OpenAI-compatible endpoint (default: no-key)",
    )
    parser.add_argument(
        "--max-tools",
        type=int,
        default=None,
        help="Limit MCP tools passed to the openai agent (reduces context size)",
    )
    parser.add_argument(
        "--no-think",
        action="store_true",
        help="Prepend /no_think to disable reasoning mode (qwen3 and compatible models)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Max output tokens per completion for openai agent (agent default: 8192)",
    )
    parser.add_argument(
        "--mcp-env",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "Extra env var for the MCP server (repeatable). "
            "ENABLE_TOOL_SEARCH defaults to true when --base-url is set "
            "(local model); override with --mcp-env ENABLE_TOOL_SEARCH=false."
        ),
    )
    parser.add_argument("extra_args", nargs="*", help="Extra args passed to run_uat.py")
    args = parser.parse_args()

    # Validate --base-url is provided when using the openai agent
    agent_list = [a.strip() for a in args.agents.split(",")]
    if "openai" in agent_list and not args.base_url:
        parser.error("--base-url is required when using the openai agent")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.all:
        stories = sorted(CATALOG_DIR.glob("s*.yaml"))
    elif args.story_file:
        story_path = Path(args.story_file).resolve()
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

    try:
        exit_code = asyncio.run(run_stories(args, filtered))
    except KeyboardInterrupt:
        log("\nInterrupted")
        sys.exit(130)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
