"""Tests that tool source code follows documentation conventions.

Legacy tag detection ensures tools use native FastMCP tags parameter.
Sync enforcement (tools.json ↔ source) is handled by the post-merge
sync-tool-docs.yml workflow rather than a PR-time unit test, because
PRs that pass CI can go stale when other tool PRs merge first.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent


class TestToolDocsSync:
    """Tool source code must follow documentation conventions."""

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
