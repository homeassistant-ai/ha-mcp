#!/usr/bin/env python3
"""
Measure MCP tool description sizes.

Connects to the ha-mcp server via FastMCP in-memory and lists all tools,
measuring description length and parameter schema size for each.

Usage:
    # Measure local (master) tools
    uv run python tests/uat/stories/scripts/measure_tools.py

    # Measure a specific branch
    uv run python tests/uat/stories/scripts/measure_tools.py --branch v6.6.1

    # Output JSON for comparison
    uv run python tests/uat/stories/scripts/measure_tools.py --output local/tool-sizes.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))


async def measure_local() -> dict:
    """Measure tools from local code via FastMCP in-memory."""
    from fastmcp import Client

    import ha_mcp.config
    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer

    ha_mcp.config._settings = None
    client = HomeAssistantClient(base_url="http://localhost:1", token="dummy")
    server = HomeAssistantSmartMCPServer(client=client)

    async with Client(server.mcp) as mcp_client:
        tools = await mcp_client.list_tools()

    tool_data = []
    total_desc = 0
    total_schema = 0
    for t in tools:
        desc_len = len(t.description or "")
        schema = json.dumps(t.inputSchema) if t.inputSchema else ""
        schema_len = len(schema)
        total_desc += desc_len
        total_schema += schema_len
        tool_data.append({
            "name": t.name,
            "desc_chars": desc_len,
            "schema_chars": schema_len,
            "total_chars": desc_len + schema_len,
        })

    tool_data.sort(key=lambda x: x["total_chars"], reverse=True)
    return {
        "total_tools": len(tools),
        "total_desc_chars": total_desc,
        "total_schema_chars": total_schema,
        "total_chars": total_desc + total_schema,
        "tools": tool_data,
    }


def measure_branch(branch: str) -> dict:
    """Measure tools from a remote branch by installing and running."""
    script = '''
import asyncio, json, sys, tempfile, shutil
from pathlib import Path

async def run():
    # Import after uvx installs
    from ha_mcp.client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from fastmcp import Client

    client = HomeAssistantClient(base_url="http://localhost:1", token="dummy")
    server = HomeAssistantSmartMCPServer(client=client)
    async with Client(server.mcp) as mcp_client:
        tools = await mcp_client.list_tools()

    tool_data = []
    total_desc = 0
    total_schema = 0
    for t in tools:
        desc_len = len(t.description or "")
        schema = json.dumps(t.inputSchema) if t.inputSchema else ""
        schema_len = len(schema)
        total_desc += desc_len
        total_schema += schema_len
        tool_data.append({
            "name": t.name,
            "desc_chars": desc_len,
            "schema_chars": schema_len,
            "total_chars": desc_len + schema_len,
        })

    tool_data.sort(key=lambda x: x["total_chars"], reverse=True)
    result = {
        "total_tools": len(tools),
        "total_desc_chars": total_desc,
        "total_schema_chars": total_schema,
        "total_chars": total_desc + total_schema,
        "tools": tool_data,
    }
    print(json.dumps(result))

asyncio.run(run())
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            [
                "uvx", "--from",
                f"git+https://github.com/homeassistant-ai/ha-mcp.git@{branch}",
                "--with", "fastmcp",
                "python", script_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"Branch measurement failed: {result.stderr}", file=sys.stderr)
            return {}
        return json.loads(result.stdout)
    except Exception as e:
        print(f"Branch measurement error: {e}", file=sys.stderr)
        return {}
    finally:
        Path(script_path).unlink(missing_ok=True)


def print_report(data: dict, label: str) -> None:
    """Print a human-readable report."""
    print(f"\n{'='*60}")
    print(f"Tool Description Sizes: {label}")
    print(f"{'='*60}")
    print(f"Total tools: {data['total_tools']}")
    print(f"Total description chars: {data['total_desc_chars']:,}")
    print(f"Total schema chars: {data['total_schema_chars']:,}")
    print(f"Total combined chars: {data['total_chars']:,}")
    print("\nTop 20 tools by size:")
    print(f"{'Tool':<45} {'Desc':>6} {'Schema':>7} {'Total':>7}")
    print("-" * 70)
    for t in data["tools"][:20]:
        print(f"{t['name']:<45} {t['desc_chars']:>6} {t['schema_chars']:>7} {t['total_chars']:>7}")


def print_comparison(v1: dict, v2: dict, label1: str, label2: str) -> None:
    """Print a comparison of two tool measurements."""
    v1_tools = {t["name"]: t for t in v1.get("tools", [])}
    v2_tools = {t["name"]: t for t in v2.get("tools", [])}
    all_names = sorted(set(list(v1_tools.keys()) + list(v2_tools.keys())))

    print(f"\n{'='*60}")
    print(f"Comparison: {label1} vs {label2}")
    print(f"{'='*60}")
    print(f"Tools: {v1.get('total_tools', 0)} -> {v2.get('total_tools', 0)}")
    print(f"Desc chars: {v1.get('total_desc_chars', 0):,} -> {v2.get('total_desc_chars', 0):,} ({v2.get('total_desc_chars', 0) - v1.get('total_desc_chars', 0):+,})")
    print(f"Schema chars: {v1.get('total_schema_chars', 0):,} -> {v2.get('total_schema_chars', 0):,}")
    print(f"Total chars: {v1.get('total_chars', 0):,} -> {v2.get('total_chars', 0):,} ({v2.get('total_chars', 0) - v1.get('total_chars', 0):+,})")

    # Show tools with changed descriptions
    changed = []
    for name in all_names:
        t1 = v1_tools.get(name, {})
        t2 = v2_tools.get(name, {})
        d1 = t1.get("desc_chars", 0)
        d2 = t2.get("desc_chars", 0)
        if d1 != d2:
            changed.append({"name": name, "old": d1, "new": d2, "delta": d2 - d1})

    if changed:
        changed.sort(key=lambda x: abs(x["delta"]), reverse=True)
        print(f"\nTools with changed descriptions ({len(changed)}):")
        print(f"{'Tool':<45} {label1:>6} {label2:>6} {'Delta':>7}")
        print("-" * 70)
        for c in changed:
            print(f"{c['name']:<45} {c['old']:>6} {c['new']:>6} {c['delta']:>+7}")

    added = [n for n in all_names if n not in v1_tools]
    removed = [n for n in all_names if n not in v2_tools]
    if added:
        print(f"\nNew tools in {label2}: {', '.join(added)}")
    if removed:
        print(f"\nRemoved in {label2}: {', '.join(removed)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure MCP tool description sizes")
    parser.add_argument("--branch", help="Compare against this branch/tag")
    parser.add_argument("--output", help="Save JSON output to file")
    parser.add_argument("--compare", help="Compare against a saved JSON file")
    args = parser.parse_args()

    data = asyncio.run(measure_local())
    print_report(data, "local (master)")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(data, indent=2))
        print(f"\nSaved to {args.output}")

    if args.compare:
        other = json.loads(Path(args.compare).read_text())
        print_comparison(other, data, Path(args.compare).stem, "local")

    if args.branch:
        print(f"\nMeasuring branch {args.branch}...")
        branch_data = measure_branch(args.branch)
        if branch_data:
            print_report(branch_data, args.branch)
            print_comparison(branch_data, data, args.branch, "local")

            if args.output:
                branch_out = args.output.replace(".json", f"-{args.branch.replace('/', '-')}.json")
                Path(branch_out).write_text(json.dumps(branch_data, indent=2))
                print(f"\nSaved branch data to {branch_out}")


if __name__ == "__main__":
    main()
