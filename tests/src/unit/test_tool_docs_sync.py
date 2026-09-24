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

TOOLS_JSON = REPO_ROOT / "site" / "src" / "data" / "tools.json"


def _tools() -> list[dict]:
    """The generated tool catalog every count in the repo is measured against."""
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


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

        tools_json = TOOLS_JSON
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
        tools = _tools()
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

    def test_hand_maintained_tool_counts_are_synced(self) -> None:
        """Each tool count listed below must match the registry.

        This is a whitelist, not a sweep: it pins the places the repo is known
        to state a count, and will not notice a new one added somewhere else.

        ``scripts/extract_tools.py`` regenerates only DOCS.md and two spans
        of README.md — the ``tools-N-blue`` badge and the ``Complete Tool
        List`` summary. Everything else below is typed by hand, and it had
        drifted in five places at once when this test was written. Asserting
        the two regenerated needles alongside the rest is deliberate: it keeps
        one list of every place the repo states a tool count, so a reader does
        not have to know which are script-maintained.

        A floor claim like "80+ tools" in the app store description is not a
        count and is not asserted here: it is written to stay true as the
        catalog grows, so pinning it to an exact figure would defeat the reason
        it is phrased that way. Every place that does state a number is below.
        """
        tools = _tools()
        count = len(tools)
        expected = {
            "README.md": [
                f"tools-{count}-blue",
                f'alt="{count} Tools"',
                f"Complete Tool List ({count} tools)",
                f"catalog (~{count} tools)",
                f"instead of {count}.",
            ],
            ".env.example": [f"catalog (~{count} tools)"],
            # Outside extract_tools' ADDON_TOOLS marker span, so hand-typed.
            # Also pinned by test_about_section_tool_count_synced above; kept
            # here so this stays one list of every place a count is stated.
            "homeassistant-addon/DOCS.md": [f"catalog (~{count} tools)"],
            "docs/FAQ.md": [f"| {count} comprehensive tools |"],
            "site/src/pages/faq.astro": [f">{count} comprehensive tools<"],
            "site/src/pages/index.astro": [f"{count} tools across 6 categories"],
            "site/src/pages/setup.astro": [
                f"adds {count} tools",
                f"all {count} tools at once",
            ],
            "tests/uat/stories/TODO.md": [f"{count}-tool codebase"],
        }
        for relative, needles in expected.items():
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for needle in needles:
                assert needle in text, (
                    f"{relative}: tool count is stale — expected {needle!r}. "
                    f"tools.json currently carries {count} tools."
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
