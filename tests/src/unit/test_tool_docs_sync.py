"""Tests that tool source code follows documentation conventions.

Legacy tag detection ensures tools use native FastMCP tags parameter.
Sync enforcement (tools.json ↔ source) is handled by the post-merge
sync-tool-docs.yml workflow rather than a PR-time unit test, because
PRs that pass CI can go stale when other tool PRs merge first.
"""

import json
import pathlib
import re
import sys
import tempfile
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

    def test_docs_has_sync_markers(self) -> None:
        """DOCS.md must contain auto-sync markers for extract_tools.py."""
        docs_path = REPO_ROOT / "homeassistant-addon" / "DOCS.md"
        assert docs_path.exists(), "homeassistant-addon/DOCS.md not found"
        docs = docs_path.read_text(encoding="utf-8")
        assert "<!-- ADDON_TOOLS_START -->" in docs, (
            "DOCS.md is missing <!-- ADDON_TOOLS_START --> marker. "
            "Run 'python scripts/extract_tools.py' to regenerate."
        )
        assert "<!-- ADDON_TOOLS_END -->" in docs, (
            "DOCS.md is missing <!-- ADDON_TOOLS_END --> marker. "
            "Run 'python scripts/extract_tools.py' to regenerate."
        )

    def test_docs_section_contains_all_tools(self) -> None:
        """Auto-generated DOCS.md section must list all tools from tools.json."""

        tools_json = REPO_ROOT / "site" / "src" / "data" / "tools.json"
        docs_path = REPO_ROOT / "homeassistant-addon" / "DOCS.md"

        tools = json.loads(tools_json.read_text(encoding="utf-8"))
        real_names = {t["name"] for t in tools}

        docs = docs_path.read_text(encoding="utf-8")
        section = re.search(
            r"<!-- ADDON_TOOLS_START -->.*?<!-- ADDON_TOOLS_END -->",
            docs,
            re.DOTALL,
        )
        assert section is not None, "Sync markers not found in DOCS.md"

        section_tools = set(re.findall(r"`(ha_[a-z0-9_]+)`", section.group(0)))
        missing = real_names - section_tools
        assert not missing, (
            f"Tools missing from DOCS.md auto-generated section ({len(missing)}): "
            + ", ".join(sorted(missing))
            + "\nRun 'python scripts/extract_tools.py' to regenerate."
        )
    def test_about_section_tool_count_synced(self) -> None:
        """update_docs() must also sync the tool count in the About section."""
        import json
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import extract_tools as et

        tools = json.loads((REPO_ROOT / "site" / "src" / "data" / "tools.json").read_text())
        expected = f"provides {len(tools)}+ tools"

        # Inject a stale count into DOCS.md content and verify update_docs fixes it
        docs = (REPO_ROOT / "homeassistant-addon" / "DOCS.md").read_text()
        stale = docs.replace(expected, "provides 1+ tools")
        # Write stale version temporarily
        original_path = et.DOCS_PATH
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
            tmp.write(stale)
            tmp_path = pathlib.Path(tmp.name)
        try:
            et.DOCS_PATH = tmp_path
            result = et.update_docs(tools)
        finally:
            et.DOCS_PATH = original_path
            tmp_path.unlink()

        assert expected in result, (
            f"update_docs() did not sync About section tool count. "
            f"Expected {expected!r} in output."
        )

