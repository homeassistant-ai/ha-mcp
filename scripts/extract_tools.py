#!/usr/bin/env python3
"""Extract MCP tool metadata via AST parsing (no runtime dependencies).

Parses tool source files statically to extract names, tags, annotations,
descriptions, and parameter schemas. Produces:
  - site/src/data/tools.json  (for Astro site tool explorer)
  - README.md update          (table between markers, badge count)

Usage:
    python scripts/extract_tools.py
    python scripts/extract_tools.py --check  # CI mode: exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "src" / "ha_mcp" / "tools"
TOOLS_JSON_PATH = REPO_ROOT / "site" / "src" / "data" / "tools.json"
README_PATH = REPO_ROOT / "README.md"
DOCS_PATH = REPO_ROOT / "homeassistant-addon" / "DOCS.md"


README_START_MARKER = "<!-- TOOLS_TABLE_START -->"
README_END_MARKER = "<!-- TOOLS_TABLE_END -->"
DOCS_START_MARKER = "<!-- ADDON_TOOLS_START -->"
DOCS_END_MARKER = "<!-- ADDON_TOOLS_END -->"

TOOL_FILES = sorted(list(TOOLS_DIR.glob("tools_*.py")) + [TOOLS_DIR / "backup.py"])

ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _extract_field_info(annotation: ast.expr | None) -> dict:
    """Extract type and description from Annotated[type, Field(...)] patterns."""
    if annotation is None:
        return {}
    info: dict = {}

    if isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Attribute) and annotation.value.attr == "Annotated":
        slice_node = annotation.slice
        if isinstance(slice_node, ast.Tuple) and slice_node.elts:
            info["type"] = ast.unparse(slice_node.elts[0])
            for elt in slice_node.elts[1:]:
                if isinstance(elt, ast.Call):
                    for kw in elt.keywords:
                        if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                            info["description"] = kw.value.value
                        elif kw.arg == "default" and isinstance(kw.value, ast.Constant):
                            info["default"] = kw.value.value
    else:
        info["type"] = ast.unparse(annotation)

    return info


def extract_tools() -> list[dict]:
    """Extract all tool metadata from source files via AST parsing."""
    tools = []

    for f in TOOL_FILES:
        if not f.exists():
            continue
        tree = ast.parse(f.read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue

            # Find the @tool or @mcp.tool decorator
            tool_dec = None
            tool_name = None
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                # Pattern 1: @mcp.tool(...) — closure pattern, function named ha_*
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    if node.name.startswith("ha_"):
                        tool_dec = dec
                        tool_name = node.name
                        break
                # Pattern 2: @tool(name="ha_*") — class method pattern
                if isinstance(func, ast.Name) and func.id == "tool":
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and str(kw.value.value).startswith("ha_"):
                            tool_dec = dec
                            tool_name = str(kw.value.value)
                            break
                    if tool_dec:
                        break

            if tool_dec is None or tool_name is None:
                continue

            dec = tool_dec
            tags: set[str] = set()
            title = ""
            annotations: dict[str, bool] = {}

            for kw in dec.keywords:
                if kw.arg == "tags" and isinstance(kw.value, ast.Set):
                    tags = {str(elt.value) for elt in kw.value.elts if isinstance(elt, ast.Constant)}
                elif kw.arg == "annotations" and isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                            key = str(k.value)
                            if key == "title":
                                title = str(v.value)
                            elif key in ANNOTATION_KEYS:
                                annotations[key] = bool(v.value)

            # Extract params with types, descriptions, defaults
            properties: dict[str, dict] = {}
            required: list[str] = []
            defaults_offset = len(node.args.args) - len(node.args.defaults)

            for i, arg in enumerate(node.args.args):
                if arg.arg in ("self", "ctx"):
                    continue
                p = _extract_field_info(arg.annotation)
                def_idx = i - defaults_offset
                if def_idx >= 0 and def_idx < len(node.args.defaults):
                    def_node = node.args.defaults[def_idx]
                    if isinstance(def_node, ast.Constant):
                        p.setdefault("default", def_node.value)
                else:
                    required.append(arg.arg)
                if p:
                    properties[arg.arg] = p

            input_schema: dict = {}
            if properties:
                input_schema = {"properties": properties}
                if required:
                    input_schema["required"] = required

            tools.append({
                "name": tool_name,
                "title": title,
                "description": ast.get_docstring(node) or "",
                "inputSchema": input_schema,
                "annotations": annotations,
                "tags": sorted(tags),
                "source_file": f.name,
            })

    tools.sort(key=lambda x: (next(iter(x["tags"]), "zzz"), x["name"]))
    return tools


def generate_docs_section(tools: list[dict]) -> str:
    """Generate the Available Tools section for homeassistant-addon/DOCS.md."""
    categories: dict[str, list[dict]] = {}
    for tool in tools:
        cat = tool["tags"][0] if tool["tags"] else "Other"
        categories.setdefault(cat, []).append(tool)

    lines = [
        DOCS_START_MARKER,
        "",
        f"The add-on provides {len(tools)}+ MCP tools for controlling Home Assistant:",
        "",
    ]
    for cat in sorted(categories):
        lines.append(f"### {cat}")
        for tool in sorted(categories[cat], key=lambda t: t["name"]):
            desc = tool["description"].split("\n")[0].strip() if tool["description"] else ""
            entry = f"- `{tool['name']}`"
            if desc:
                entry += f" — {desc}"
            lines.append(entry)
        lines.append("")
    lines.append(DOCS_END_MARKER)
    return "\n".join(lines)


def update_docs(tools: list[dict], *, content: str | None = None) -> str:
    """Replace the auto-generated section in DOCS.md between sync markers.

    Args:
        tools: Extracted tool metadata.
        content: File content to use instead of reading DOCS_PATH from disk.
            Pass this when the caller has already read the file (e.g. check_sync)
            to avoid a redundant read. When None, reads DOCS_PATH internally.
    """
    docs = content if content is not None else DOCS_PATH.read_text(encoding="utf-8")
    if DOCS_START_MARKER not in docs or DOCS_END_MARKER not in docs:
        print(
            f"ERROR: {DOCS_PATH} is missing sync markers.\n"
            f"  Add {DOCS_START_MARKER!r} and {DOCS_END_MARKER!r} to the file first.",
            file=sys.stderr,
        )
        sys.exit(1)
    new_section = generate_docs_section(tools)
    pattern = re.compile(
        rf"{re.escape(DOCS_START_MARKER)}.*?{re.escape(DOCS_END_MARKER)}",
        re.DOTALL,
    )
    updated = pattern.sub(new_section, docs)
    updated = re.sub(r"\bprovides \d+\+ tools\b", f"provides {len(tools)}+ tools", updated)
    updated = re.sub(r"\bcatalog \(~\d+ tools\b", f"catalog (~{len(tools)} tools", updated)
    assert DOCS_START_MARKER in updated and DOCS_END_MARKER in updated
    return updated


def generate_tools_json(tools: list[dict]) -> str:
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def generate_readme_table(tools: list[dict]) -> str:
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
    lines.extend(
        f"| **{cat}** | {', '.join(sorted(categories[cat]))} |"
        for cat in sorted(categories)
    )
    lines.extend(["", README_END_MARKER])
    return "\n".join(lines)


def update_readme(tools: list[dict], *, content: str | None = None) -> str:
    """Replace the tool table in README.md between markers.

    Args:
        tools: Extracted tool metadata.
        content: File content to use instead of reading README_PATH from disk.
            Pass this when the caller has already read the file (e.g. check_sync)
            to avoid a redundant read. When None, reads README_PATH internally.
    """
    readme = content if content is not None else README_PATH.read_text(encoding="utf-8")
    table = generate_readme_table(tools)
    count = len(tools)

    pattern = re.compile(
        rf"<details>\s*\n{re.escape(README_START_MARKER)}.*?{re.escape(README_END_MARKER)}\s*\n</details>",
        re.DOTALL,
    )
    new_block = f"<details>\n{table}\n</details>"

    if pattern.search(readme):
        readme = pattern.sub(new_block, readme)
    else:
        old_pattern = re.compile(
            r"<details>\s*\n<summary><b>[^<]*Complete Tool List[^<]*</b></summary>.*?</details>",
            re.DOTALL,
        )
        if old_pattern.search(readme):
            readme = old_pattern.sub(new_block, readme)
        else:
            print("WARNING: Could not find tool table markers in README.md", file=sys.stderr)
            return readme

    readme = re.sub(r"tools-[^-]+-blue", f"tools-{count}-blue", readme)
    return readme


def check_sync(tools: list[dict]) -> bool:
    in_sync = True

    expected_json = generate_tools_json(tools)
    if TOOLS_JSON_PATH.exists():
        if TOOLS_JSON_PATH.read_text() != expected_json:
            print("OUT OF SYNC: site/src/data/tools.json", file=sys.stderr)
            in_sync = False
    else:
        print("MISSING: site/src/data/tools.json", file=sys.stderr)
        in_sync = False

    readme_content = README_PATH.read_text(encoding="utf-8")
    if readme_content != update_readme(tools, content=readme_content):
        print("OUT OF SYNC: README.md", file=sys.stderr)
        in_sync = False

    if DOCS_PATH.exists():
        docs_content = DOCS_PATH.read_text(encoding="utf-8")
        if docs_content != update_docs(tools, content=docs_content):
            print("OUT OF SYNC: homeassistant-addon/DOCS.md", file=sys.stderr)
            in_sync = False

    return in_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract MCP tool metadata (AST-based, no runtime deps)")
    parser.add_argument("--check", action="store_true", help="CI mode: check sync without writing")
    args = parser.parse_args()

    tools = extract_tools()
    cat_count = len({t["tags"][0] for t in tools if t["tags"]})
    print(f"Extracted {len(tools)} tools across {cat_count} categories")

    if args.check:
        if check_sync(tools):
            print("All files in sync.")
        else:
            print("\nRun 'python scripts/extract_tools.py' to regenerate.", file=sys.stderr)
            sys.exit(1)
    else:
        TOOLS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOOLS_JSON_PATH.write_text(generate_tools_json(tools))
        print(f"Wrote {TOOLS_JSON_PATH.relative_to(REPO_ROOT)}")

        README_PATH.write_text(update_readme(tools), encoding="utf-8")
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")

        DOCS_PATH.write_text(update_docs(tools), encoding="utf-8")
        print(f"Updated {DOCS_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
