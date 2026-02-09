"""
Parametrized story tests.

Discovers all YAML stories in catalog/, runs setup via FastMCP,
executes the test prompt via BAT runner, and cleans up via FastMCP.

Usage:
    uv run pytest tests/uat/stories/ -v
    uv run pytest tests/uat/stories/ -v -k "automation"
    uv run pytest tests/uat/stories/ -v -k "s01"
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import discover_stories, run_setup_steps, run_teardown_steps

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RUN_UAT = REPO_ROOT / "tests" / "uat" / "run_uat.py"

# Discover stories at import time for parametrization
_stories = discover_stories()


def _story_id(story: dict) -> str:
    """Generate a readable test ID from a story."""
    return f"{story['id']}_{story['category']}"


@pytest.mark.parametrize("story", _stories, ids=[_story_id(s) for s in _stories])
class TestStory:
    """Run a single user acceptance story."""

    async def test_story(self, story: dict, mcp_client, ha_container):
        """
        Execute a story: setup → agent prompt → teardown.

        The setup and teardown run via FastMCP in-memory (fast, deterministic).
        The test prompt runs via the BAT runner (real AI agent interaction).
        """
        story_id = story["id"]
        title = story["title"]
        prompt = story["prompt"]
        setup_steps = story.get("setup") or []
        teardown_steps = story.get("teardown") or []

        logger.info(f"{'='*60}")
        logger.info(f"Story {story_id}: {title}")
        logger.info(f"{'='*60}")

        # --- Setup via FastMCP ---
        if setup_steps:
            logger.info(f"[{story_id}] Running {len(setup_steps)} setup steps...")
            await run_setup_steps(mcp_client, setup_steps)

        try:
            # --- Test via BAT runner ---
            logger.info(f"[{story_id}] Running agent prompt via BAT...")

            scenario = {"test_prompt": prompt.strip()}

            # Determine which agents are available
            agent = _detect_agent()
            if not agent:
                pytest.skip("No AI agent CLI available (need 'claude' or 'gemini')")

            result = _run_bat_scenario(
                scenario,
                agent=agent,
                ha_url=ha_container["url"],
                ha_token=ha_container["token"],
            )

            # --- Evaluate ---
            _evaluate_result(story, result, agent)

        finally:
            # --- Teardown via FastMCP ---
            if teardown_steps:
                logger.info(f"[{story_id}] Running {len(teardown_steps)} teardown steps...")
                await run_teardown_steps(mcp_client, teardown_steps)


def _detect_agent() -> str | None:
    """Detect which AI agent CLI is available."""
    import shutil

    for agent in ("gemini", "claude"):
        if shutil.which(agent):
            return agent
    return None


def _run_bat_scenario(
    scenario: dict, agent: str, ha_url: str, ha_token: str
) -> dict:
    """Run a BAT scenario via run_uat.py and return parsed results."""
    cmd = [
        sys.executable,
        str(RUN_UAT),
        "--agents", agent,
        "--ha-url", ha_url,
        "--ha-token", ha_token,
        "--timeout", "180",
    ]

    result = subprocess.run(
        cmd,
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.error(f"BAT runner failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "agents": {agent: {"available": False, "all_passed": False}},
            "raw_stdout": result.stdout,
            "raw_stderr": result.stderr,
        }


def _evaluate_result(story: dict, result: dict, agent: str) -> None:
    """Evaluate BAT results against story expectations."""
    agent_result = result.get("agents", {}).get(agent, {})

    if not agent_result.get("available", False):
        pytest.skip(f"Agent '{agent}' not available")

    # Basic pass: agent completed without errors
    all_passed = agent_result.get("all_passed", False)
    test_phase = agent_result.get("test", {})

    if not test_phase.get("completed", False):
        stderr = test_phase.get("stderr", "")
        output = test_phase.get("output", "")
        pytest.fail(
            f"Story {story['id']} failed: agent did not complete.\n"
            f"Exit code: {test_phase.get('exit_code')}\n"
            f"Stderr: {stderr[:500]}\n"
            f"Output: {output[:500]}"
        )

    # Log metrics for comparison
    logger.info(
        f"  Completed in {test_phase.get('duration_ms', 0)}ms, "
        f"turns={test_phase.get('num_turns', '?')}, "
        f"tool_calls={test_phase.get('tool_stats', {}).get('totalCalls', '?')}"
    )

    # Check aggregate stats
    aggregate = agent_result.get("aggregate", {})
    if aggregate:
        logger.info(
            f"  Aggregate: {aggregate.get('total_tool_calls', 0)} tool calls, "
            f"{aggregate.get('total_tool_fail', 0)} failures"
        )

    assert all_passed, (
        f"Story {story['id']} did not pass all phases. "
        f"Check results_file: {result.get('results_file', 'N/A')}"
    )
