#!/usr/bin/env python3
"""
Standalone story runner.

Converts YAML stories to BAT format and runs them via run_uat.py.
In standalone mode, setup/teardown are converted to agent prompts
(since there's no FastMCP in-memory client available).

Results are appended to a JSONL file for historical tracking.

Usage:
    # Run a single story
    python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini

    # Run all stories
    python tests/uat/stories/run_story.py --all --agents gemini

    # Run against a specific branch/tag
    python tests/uat/stories/run_story.py --all --agents gemini --branch v6.6.1

    # Just print the BAT scenario JSON (for piping to run_uat.py manually)
    python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CATALOG_DIR = SCRIPT_DIR / "catalog"
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
RUN_UAT = SCRIPT_DIR.parent / "run_uat.py"
DEFAULT_RESULTS_FILE = REPO_ROOT / "local" / "uat-results.jsonl"


def load_story(path: Path) -> dict:
    """Load a story YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def story_to_bat_scenario(story: dict) -> dict:
    """Convert a story to BAT scenario format.

    In standalone mode, setup/teardown steps are converted to agent prompts
    that describe the MCP tool calls to make.
    """
    scenario: dict = {}

    # Convert setup steps to a prompt
    setup_steps = story.get("setup") or []
    if setup_steps:
        setup_lines = ["Set up the test environment by running these tool calls:"]
        for step in setup_steps:
            tool = step["tool"]
            args = step.get("args", {})
            args_str = json.dumps(args) if args else ""
            setup_lines.append(f"- Call {tool} with arguments: {args_str}")
        setup_lines.append("Report what you created.")
        scenario["setup_prompt"] = "\n".join(setup_lines)

    # Test prompt is used directly
    scenario["test_prompt"] = story["prompt"].strip()

    # Convert teardown steps to a prompt
    teardown_steps = story.get("teardown") or []
    if teardown_steps:
        teardown_lines = ["Clean up the test environment:"]
        for step in teardown_steps:
            tool = step["tool"]
            args = step.get("args", {})
            args_str = json.dumps(args) if args else ""
            teardown_lines.append(f"- Call {tool} with arguments: {args_str}")
        teardown_lines.append("Report what you cleaned up.")
        scenario["teardown_prompt"] = "\n".join(teardown_lines)

    return scenario


def get_git_info() -> tuple[str, str]:
    """Get git commit SHA (short) and human-readable description.

    Returns (sha, describe) where describe is like "v6.6.1" or "v6.6.1-5-gabc1234".
    """
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


def append_result(
    results_file: Path,
    story: dict,
    agent: str,
    sha: str,
    describe: str,
    branch: str | None,
    bat_summary: dict,
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
        "duration_ms": aggregate.get("total_duration_ms", test_phase.get("duration_ms")),
        "tool_calls": aggregate.get("total_tool_calls"),
        "tool_failures": aggregate.get("total_tool_fail"),
        "turns": test_phase.get("num_turns"),
    }

    results_file.parent.mkdir(parents=True, exist_ok=True)
    with open(results_file, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def run_story(
    story_path: Path,
    agents: str,
    branch: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, dict | None]:
    """Run a single story via run_uat.py. Returns (exit_code, parsed_summary)."""
    story = load_story(story_path)
    scenario = story_to_bat_scenario(story)

    print(f"--- Story {story['id']}: {story['title']} ---", file=sys.stderr)
    print(f"Category: {story['category']}, Weight: {story['weight']}", file=sys.stderr)

    cmd = [
        sys.executable,
        str(RUN_UAT),
        "--agents", agents,
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

    # Print stderr passthrough
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

    # Parse JSON summary from stdout
    summary = None
    if result.stdout.strip():
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    return result.returncode, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run user acceptance stories via BAT")
    parser.add_argument("story_file", nargs="?", help="Path to story YAML file")
    parser.add_argument("--all", action="store_true", help="Run all stories in catalog/")
    parser.add_argument("--agents", default="gemini", help="Comma-separated agent list")
    parser.add_argument("--branch", help="Git branch/tag to install ha-mcp from")
    parser.add_argument("--dry-run", action="store_true", help="Print BAT JSON without running")
    parser.add_argument("--min-weight", type=int, default=1, help="Minimum story weight to run")
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
        help=f"JSONL file to append results to (default: {DEFAULT_RESULTS_FILE})",
    )
    parser.add_argument("extra_args", nargs="*", help="Extra args passed to run_uat.py")
    args = parser.parse_args()

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
        for path, story in filtered:
            scenario = story_to_bat_scenario(story)
            print(f"# {story['id']}: {story['title']}")
            print(json.dumps(scenario, indent=2))
            print()
        return

    sha, describe = get_git_info()
    agent_list = [a.strip() for a in args.agents.split(",")]

    # Run stories
    results = []
    for path, story in filtered:
        rc, summary = run_story(path, args.agents, args.branch, args.extra_args or None)
        results.append((story, rc, summary))

        # Append JSONL result for each agent
        if summary:
            for agent in agent_list:
                append_result(
                    args.results_file, story, agent, sha, describe,
                    args.branch, summary,
                )

    # Summary
    print(f"\n--- Summary ---", file=sys.stderr)
    for story, rc, _ in results:
        status = "PASS" if rc == 0 else "FAIL"
        print(f"  [{status}] {story['id']}: {story['title']}", file=sys.stderr)

    print(f"\nResults appended to {args.results_file}", file=sys.stderr)

    failed = sum(1 for _, rc, _ in results if rc != 0)
    if failed:
        print(f"\n{failed}/{len(results)} stories failed", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\nAll {len(results)} stories passed", file=sys.stderr)


if __name__ == "__main__":
    main()
