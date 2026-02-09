#!/usr/bin/env python3
"""
Standalone story runner.

Converts YAML stories to BAT format and runs them via run_uat.py.
In standalone mode, setup/teardown are converted to agent prompts
(since there's no FastMCP in-memory client available).

Usage:
    # Run a single story
    python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini

    # Run all stories
    python tests/uat/stories/run_story.py --all --agents gemini

    # Just print the BAT scenario JSON (for piping to run_uat.py manually)
    python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CATALOG_DIR = SCRIPT_DIR / "catalog"
RUN_UAT = SCRIPT_DIR.parent / "run_uat.py"


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


def run_story(story_path: Path, agents: str, extra_args: list[str] | None = None) -> int:
    """Run a single story via run_uat.py."""
    story = load_story(story_path)
    scenario = story_to_bat_scenario(story)

    print(f"--- Story {story['id']}: {story['title']} ---", file=sys.stderr)
    print(f"Category: {story['category']}, Weight: {story['weight']}", file=sys.stderr)

    cmd = [
        sys.executable,
        str(RUN_UAT),
        "--agents", agents,
    ]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        input=json.dumps(scenario),
        capture_output=False,
        text=True,
    )

    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run user acceptance stories via BAT")
    parser.add_argument("story_file", nargs="?", help="Path to story YAML file")
    parser.add_argument("--all", action="store_true", help="Run all stories in catalog/")
    parser.add_argument("--agents", default="gemini", help="Comma-separated agent list")
    parser.add_argument("--dry-run", action="store_true", help="Print BAT JSON without running")
    parser.add_argument("--min-weight", type=int, default=1, help="Minimum story weight to run")
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

    # Run stories
    results = []
    for path, story in filtered:
        rc = run_story(path, args.agents, args.extra_args or None)
        results.append((story["id"], story["title"], rc))

    # Summary
    print("\n--- Summary ---", file=sys.stderr)
    for sid, title, rc in results:
        status = "PASS" if rc == 0 else "FAIL"
        print(f"  [{status}] {sid}: {title}", file=sys.stderr)

    failed = sum(1 for _, _, rc in results if rc != 0)
    if failed:
        print(f"\n{failed}/{len(results)} stories failed", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\nAll {len(results)} stories passed", file=sys.stderr)


if __name__ == "__main__":
    main()
