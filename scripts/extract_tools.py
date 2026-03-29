#!/usr/bin/env python3
"""Extract MCP tool metadata and generate documentation artifacts.

Instantiates the ha-mcp server with a dummy client (no HA connection needed),
calls list_tools() to get full schemas, and produces:
  - site/src/data/tools.json  (for Astro site tool explorer)
  - README.md update          (table between markers, badge count)

Usage:
    uv run python scripts/extract_tools.py
    uv run python scripts/extract_tools.py --check  # CI mode: exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

TOOLS_JSON_PATH = REPO_ROOT / "site" / "src" / "data" / "tools.json"
README_PATH = REPO_ROOT / "README.md"

README_START_MARKER = "<!-- TOOLS_TABLE_START -->"
README_END_MARKER = "<!-- TOOLS_TABLE_END -->"

# Map source module to tool function names (populated at runtime)
_SOURCE_MAP: dict[str, str] = {}


def _build_source_map() -> None:
    """Scan tools/ directory to map function names to source files."""
    import ast

    tools_dir = REPO_ROOT / "src" / "ha_mcp" / "tools"
    for py_file in list(tools_dir.glob("tools_*.py")) + [tools_dir / "backup.py"]:
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("ha_"):
                _SOURCE_MAP[node.name] = py_file.name


async def extract_tools() -> list[dict]:
    """Extract all tool metadata from the MCP server.

    Uses server.mcp.list_tools() directly (not via MCP Client) to access
    FastMCP's native tags which aren't part of the MCP protocol.

    Enables all feature flags to capture every tool.
    """
    import os

    import ha_mcp.config
    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer

    # Enable all feature-flagged tools for complete extraction
    os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"
    os.environ["HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"] = "true"

    ha_mcp.config._settings = None
    client = HomeAssistantClient(base_url="http://localhost:1", token="dummy")
    server = HomeAssistantSmartMCPServer(client=client)

    tools = await server.mcp.list_tools()

    _build_source_map()

    result = []
    for t in tools:
        annotations = {}
        if t.annotations:
            annotations = {
                k: v
                for k, v in {
                    "readOnlyHint": t.annotations.readOnlyHint,
                    "destructiveHint": t.annotations.destructiveHint,
                    "idempotentHint": t.annotations.idempotentHint,
                    "openWorldHint": t.annotations.openWorldHint,
                }.items()
                if v is not None
            }

        tags = sorted(t.tags) if t.tags else []

        # Get JSON Schema and title via MCP tool conversion
        mcp_tool = t.to_mcp_tool()
        input_schema = mcp_tool.inputSchema or {}

        result.append({
            "name": t.name,
            "title": mcp_tool.title or t.title or "",
            "description": t.description or "",
            "inputSchema": input_schema,
            "annotations": annotations,
            "tags": tags,
            "source_file": _SOURCE_MAP.get(t.name, ""),
        })

    result.sort(key=lambda x: (x["tags"][0] if x["tags"] else "zzz", x["name"]))
    return result


def generate_tools_json(tools: list[dict]) -> str:
    """Generate tools.json content."""
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def generate_readme_table(tools: list[dict]) -> str:
    """Generate the Markdown table for README, grouped by category."""
    categories: dict[str, list[str]] = {}
    for tool in tools:
        cat = tool["tags"][0] if tool["tags"] else "Other"
        categories.setdefault(cat, []).append(f"`{tool['name']}`")

    lines = [
        README_START_MARKER,
        "",
        f'<summary><b>Complete Tool List ({len(tools)} tools)</b></summary>',
        "",
        "| Category | Tools |",
        "|----------|-------|",
    ]

    for cat in sorted(categories):
        tool_list = ", ".join(sorted(categories[cat]))
        lines.append(f"| **{cat}** | {tool_list} |")

    lines.extend(["", README_END_MARKER])
    return "\n".join(lines)


def update_readme(tools: list[dict]) -> str:
    """Return updated README content with new tool table and badge count."""
    readme = README_PATH.read_text()
    table = generate_readme_table(tools)
    count = len(tools)

    # Replace between markers (keep <details> wrapper)
    pattern = re.compile(
        rf"<details>\s*\n{re.escape(README_START_MARKER)}.*?{re.escape(README_END_MARKER)}\s*\n</details>",
        re.DOTALL,
    )

    new_block = f"<details>\n{table}\n</details>"

    if pattern.search(readme):
        readme = pattern.sub(new_block, readme)
    else:
        # First time: replace existing <details> block with tool list
        old_pattern = re.compile(
            r"<details>\s*\n<summary><b>[^<]*Complete Tool List[^<]*</b></summary>.*?</details>",
            re.DOTALL,
        )
        if old_pattern.search(readme):
            readme = old_pattern.sub(new_block, readme)
        else:
            print("WARNING: Could not find tool table markers in README.md", file=sys.stderr)
            return readme

    # Update badge count
    readme = re.sub(
        r'tools-\d+\+-blue',
        f'tools-{count}-blue',
        readme,
    )

    return readme


def check_sync(tools: list[dict]) -> bool:
    """Check if generated files are in sync with code. Returns True if in sync."""
    in_sync = True

    # Check tools.json
    expected_json = generate_tools_json(tools)
    if TOOLS_JSON_PATH.exists():
        current_json = TOOLS_JSON_PATH.read_text()
        if current_json != expected_json:
            print("OUT OF SYNC: site/src/data/tools.json", file=sys.stderr)
            in_sync = False
    else:
        print("MISSING: site/src/data/tools.json", file=sys.stderr)
        in_sync = False

    # Check README
    expected_readme = update_readme(tools)
    current_readme = README_PATH.read_text()
    if current_readme != expected_readme:
        print("OUT OF SYNC: README.md", file=sys.stderr)
        in_sync = False

    return in_sync


async def main() -> None:
    parser = argparse.ArgumentParser(description="Extract MCP tool metadata")
    parser.add_argument("--check", action="store_true", help="CI mode: check sync without writing")
    args = parser.parse_args()

    tools = await extract_tools()
    print(f"Extracted {len(tools)} tools across {len({t['tags'][0] for t in tools if t['tags']})} categories")

    if args.check:
        if check_sync(tools):
            print("All files in sync.")
        else:
            print("\nRun 'uv run python scripts/extract_tools.py' to regenerate.", file=sys.stderr)
            sys.exit(1)
    else:
        # Write tools.json
        TOOLS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOOLS_JSON_PATH.write_text(generate_tools_json(tools))
        print(f"Wrote {TOOLS_JSON_PATH.relative_to(REPO_ROOT)}")

        # Update README
        README_PATH.write_text(update_readme(tools))
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
