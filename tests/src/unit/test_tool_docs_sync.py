"""Tests that tool documentation artifacts stay in sync with source code.

Validates:
1. Every tool has a native FastMCP tag (category)
2. README.md and tools.json are up to date

Run `uv run python scripts/extract_tools.py` to regenerate if this fails.
"""

import asyncio
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent


def get_tools_dir() -> Path:
    return REPO_ROOT / "src" / "ha_mcp" / "tools"


def extract_tool_tags(file_path: Path) -> list[dict]:
    """Extract @mcp.tool tag information from a Python file."""
    content = file_path.read_text(encoding="utf-8")
    tools = []

    pattern = r"@mcp\.tool\((.*?)\)\s*(?:@\w+.*?\n\s*)*async def (\w+)"
    for match in re.finditer(pattern, content, re.DOTALL):
        decorator_args = match.group(1)
        func_name = match.group(2)
        has_tags = "tags=" in decorator_args or "tags =" in decorator_args
        tools.append({
            "file": file_path.name,
            "function": func_name,
            "has_tags": has_tags,
        })

    return tools


class TestToolTags:
    """Every MCP tool must have a native FastMCP tags= parameter."""

    def test_all_tools_have_tags(self):
        tools_dir = get_tools_dir()
        files = list(tools_dir.glob("tools_*.py")) + [tools_dir / "backup.py"]
        missing = []

        for f in sorted(files):
            if not f.exists():
                continue
            for tool in extract_tool_tags(f):
                if not tool["has_tags"]:
                    missing.append(f"{tool['function']} ({tool['file']})")

        assert not missing, (
            f"{len(missing)} tool(s) missing tags= parameter:\n"
            + "\n".join(f"  - {m}" for m in missing)
            + "\n\nAdd tags={'Category Name'} to each @mcp.tool() decorator."
        )

    def test_no_legacy_tags_in_annotations(self):
        """Tags should be native FastMCP parameter, not inside annotations dict."""
        tools_dir = get_tools_dir()
        files = list(tools_dir.glob("tools_*.py")) + [tools_dir / "backup.py"]
        legacy = []

        for f in sorted(files):
            if not f.exists():
                continue
            content = f.read_text(encoding="utf-8")
            for match in re.finditer(r'"tags"\s*:', content):
                legacy.append(f"{f.name}:{match.start()}")

        assert not legacy, (
            f"Found legacy \"tags\" inside annotations dict in {len(legacy)} location(s):\n"
            + "\n".join(f"  - {l}" for l in legacy)
            + "\n\nUse tags={'Category'} as a direct @mcp.tool() parameter instead."
        )


class TestToolDocsSync:
    """README.md and tools.json must stay in sync with tool source code."""

    def test_docs_in_sync(self):
        """Verify generated artifacts match current tool definitions.

        If this fails, run: uv run python scripts/extract_tools.py
        """
        import subprocess

        result = subprocess.run(
            ["uv", "run", "python", "scripts/extract_tools.py", "--check"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )

        assert result.returncode == 0, (
            "Tool documentation is out of sync with source code.\n\n"
            + result.stderr
            + "\nRun this command to fix:\n"
            + "  uv run python scripts/extract_tools.py\n"
        )
