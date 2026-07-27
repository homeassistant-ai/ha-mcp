"""Tests that tool source code follows documentation conventions.

Legacy tag detection ensures tools use native FastMCP tags parameter.
Sync enforcement (tools.json ↔ source) is handled by the post-merge
sync-tool-docs.yml workflow rather than a PR-time unit test, because
PRs that pass CI can go stale when other tool PRs merge first.
"""

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# The generator is a script, not a package module — same import route the
# locale-parity checks use.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402


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
            f'Found legacy "tags" inside annotations dict in {len(legacy)} location(s):\n'
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

        # Pattern targets "- `ha_xxx`" at line start (re.MULTILINE).
        # Assumes tool entries are never indented; update regex if format changes.
        section_tools = set(
            re.findall(r"^- `(ha_[a-z0-9_]+)`", section.group(0), re.MULTILINE)
        )
        missing = real_names - section_tools
        assert not missing, (
            f"Tools missing from DOCS.md auto-generated section ({len(missing)}): "
            + ", ".join(sorted(missing))
            + "\nRun 'python scripts/extract_tools.py' to regenerate."
        )

        extra = section_tools - real_names
        assert not extra, (
            f"Ghost tools found in DOCS.md auto-generated section ({len(extra)}): "
            + ", ".join(sorted(extra))
            + "\nRun 'python scripts/extract_tools.py' to regenerate."
        )

    def test_about_section_tool_count_synced(self) -> None:
        """Tool count in About section must match the actual tool registry."""
        tools = json.loads(
            (REPO_ROOT / "site" / "src" / "data" / "tools.json").read_text(
                encoding="utf-8"
            )
        )
        docs = (REPO_ROOT / "homeassistant-addon" / "DOCS.md").read_text(
            encoding="utf-8"
        )
        for expected in [
            f"provides {len(tools)}+ tools",
            f"catalog (~{len(tools)} tools",
        ]:
            assert expected in docs, (
                f"Tool count {expected!r} is stale in DOCS.md. "
                "Run 'python scripts/extract_tools.py' to regenerate."
            )


class TestExtractToolsScriptRobustness:
    """Structural guards on scripts/extract_tools.py itself.

    Two review rounds fixed the same missing-``encoding`` defect in this file,
    one of them on the destructive write. Every call is correct today and
    nothing kept the next one honest, so the rule is asserted over the source
    rather than re-checked by hand.
    """

    SCRIPT = REPO_ROOT / "scripts" / "extract_tools.py"

    def test_every_file_read_and_write_declares_an_encoding(self) -> None:
        """No ``read_text``/``write_text``/``open`` may inherit the locale.

        Without ``encoding``, Python uses the platform default, so the script
        reads and writes the tool catalogs — which carry non-ASCII by design
        (``generate_tools_json`` emits ``ensure_ascii=False``) — in whatever
        the runner's locale happens to be.
        """
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        checked = 0
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name not in {"read_text", "write_text", "open"}:
                continue
            checked += 1
            if not any(kw.arg == "encoding" for kw in node.keywords):
                offenders.append(f"{name}() at line {node.lineno}")

        # Count parity: a parse that stopped finding calls would otherwise
        # report "no offenders" and read as a pass.
        assert checked >= 9, (
            f"only {checked} file-IO calls found in {self.SCRIPT.name} — the "
            "check below would pass by inspecting almost nothing"
        )
        assert not offenders, (
            f"{self.SCRIPT.name} has {len(offenders)} file-IO call(s) without "
            f"an explicit encoding: {offenders}. Pass encoding='utf-8'."
        )

    def test_lost_readme_markers_fail_instead_of_reporting_in_sync(self) -> None:
        """A README whose markers are gone must not compare equal to itself.

        Returning the content unchanged made ``--check`` print "All files in
        sync" right after printing the warning that it could not find the
        markers — the verification path passing on its own failure.
        """
        with pytest.raises(ValueError, match="tool-table markers"):
            extract_tools.update_readme([], content="# README\n\nNo markers here.\n")

    def test_lost_docs_markers_raise_rather_than_exit(self) -> None:
        """The sibling failure, reported the same way.

        A library function that calls ``sys.exit`` hands its caller a bare
        SystemExit to report instead of a named cause.
        """
        with pytest.raises(ValueError, match="sync markers"):
            extract_tools.update_docs([], content="# DOCS\n\nNo markers here.\n")
