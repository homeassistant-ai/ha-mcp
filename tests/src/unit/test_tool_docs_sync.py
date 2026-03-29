"""Tests that tool documentation artifacts stay in sync with source code.

Run `uv run python scripts/extract_tools.py` to regenerate if this fails.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent


class TestToolDocsSync:
    """README.md and tools.json must stay in sync with tool source code."""

    def test_no_legacy_tags_in_annotations(self):
        """Tags should be native FastMCP parameter, not inside annotations dict."""
        tools_dir = REPO_ROOT / "src" / "ha_mcp" / "tools"
        files = list(tools_dir.glob("tools_*.py")) + [tools_dir / "backup.py"]
        legacy = []

        for f in sorted(files):
            if not f.exists():
                continue
            content = f.read_text(encoding="utf-8")
            legacy.extend(
                f"{f.name}:{match.start()}"
                for match in re.finditer(r'"tags"\s*:', content)
            )

        assert not legacy, (
            f"Found legacy \"tags\" inside annotations dict in {len(legacy)} location(s):\n"
            + "\n".join(f"  - {loc}" for loc in legacy)
            + "\n\nUse tags={'Category'} as a direct @mcp.tool() parameter instead."
        )

    def test_docs_in_sync(self):
        """Verify generated artifacts match current tool definitions.

        If this fails, run: uv run python scripts/extract_tools.py
        """
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
