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
ADDON_DIR = REPO_ROOT / "homeassistant-addon"
ADDON_DEV_DIR = REPO_ROOT / "homeassistant-addon-dev"

README_START_MARKER = "<!-- TOOLS_TABLE_START -->"
README_END_MARKER = "<!-- TOOLS_TABLE_END -->"

# Markers for generated tool sections in config.yaml
CONFIG_TOOLS_START = "  # --- GENERATED TOOL CONFIG START (do not edit) ---"
CONFIG_TOOLS_END = "  # --- GENERATED TOOL CONFIG END ---"
SCHEMA_TOOLS_START = "  # --- GENERATED TOOL SCHEMA START (do not edit) ---"
SCHEMA_TOOLS_END = "  # --- GENERATED TOOL SCHEMA END ---"

# Tools that are pinned by default (always visible in tool search)
DEFAULT_PINNED = {
    "ha_restart", "ha_reload_core", "ha_backup_create", "ha_backup_restore",
    "ha_get_overview", "ha_report_issue", "ha_search_entities",
    "ha_config_get_automation", "ha_config_set_automation", "ha_config_set_yaml",
}

# Tools that cannot be disabled (mandatory)
MANDATORY_TOOLS = {"ha_search_entities", "ha_get_overview", "ha_get_state", "ha_report_issue"}

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
            if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("ha_"):
                continue

            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (isinstance(func, ast.Attribute) and func.attr == "tool"):
                    continue

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
                    "name": node.name,
                    "title": title,
                    "description": ast.get_docstring(node) or "",
                    "inputSchema": input_schema,
                    "annotations": annotations,
                    "tags": sorted(tags),
                    "source_file": f.name,
                })
                break

    tools.sort(key=lambda x: (next(iter(x["tags"]), "zzz"), x["name"]))
    return tools


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


def update_readme(tools: list[dict]) -> str:
    readme = README_PATH.read_text()
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


def _group_slug(tag: str) -> str:
    """Convert a tag like 'Areas & Floors' to a slug like 'areas_floors'."""
    return tag.lower().replace(" & ", "_").replace(" ", "_")


def _group_tools(tools: list[dict]) -> dict[str, list[dict]]:
    """Group tools by their first tag, sorted by tag name."""
    groups: dict[str, list[dict]] = {}
    for tool in tools:
        tag = tool["tags"][0] if tool["tags"] else "Other"
        groups.setdefault(tag, []).append(tool)
    return dict(sorted(groups.items()))


_TOOL_STATES = "enabled-unpinned|enabled-pinned|disabled"


def _default_state(tool: dict) -> str:
    """Return the default state for a tool."""
    if tool["name"] in MANDATORY_TOOLS or tool["name"] in DEFAULT_PINNED:
        return "enabled-pinned"
    return "enabled-unpinned"



def generate_addon_config_tools(tools: list[dict]) -> tuple[str, str]:
    """Generate the options and schema sections for tools in config.yaml.

    Tools are nested under a single 'tools:' parent for one collapsible section.
    Each tool uses list(unpinned|pinned|disabled) dropdown.
    Returns (options_block, schema_block) as strings to insert between markers.
    """
    groups = _group_tools(tools)
    opt_lines = [CONFIG_TOOLS_START, "  tools:"]
    sch_lines = [SCHEMA_TOOLS_START, "  tools:"]

    for tag, group_tools in groups.items():
        slug = _group_slug(tag)
        opt_lines.append(f"    {slug}:")
        opt_lines.append("      enabled: true")
        sch_lines.append(f"    {slug}:")
        sch_lines.append("      enabled: bool?")
        for tool in group_tools:
            state = _default_state(tool)
            opt_lines.append(f"      {tool['name']}: \"{state}\"")
            sch_lines.append(f"      {tool['name']}: \"list({_TOOL_STATES})?\"")

    opt_lines.append(CONFIG_TOOLS_END)
    sch_lines.append(SCHEMA_TOOLS_END)
    return "\n".join(opt_lines), "\n".join(sch_lines)


def generate_addon_translations(tools: list[dict]) -> str:
    """Generate the translations/en.yaml for the addon.

    Only top-level config keys get translations (Supervisor doesn't
    render translations deeper than 1 level under configuration).
    """
    lines = [
        "---",
        "configuration:",
        "  backup_hint:",
        "    name: Backup hint",
        "    description: Controls when backup reminders are shown before risky operations.",
        "  secret_path:",
        "    name: Secret path override",
        "    description: |",
        "      Optional custom HTTP path for the MCP server. Leave empty to use the auto-generated secure path.",
        "  enable_skills:",
        "    name: Enable skills",
        "    description: >-",
        "      Serve bundled Home Assistant best-practice skills as MCP resources.",
        "      Skills provide automation patterns, helper selection guides, and device",
        "      control best practices. Clients must explicitly request them.",
        "  enable_skills_as_tools:",
        "    name: Enable skills as tools",
        "    description: >-",
        "      Expose skills via list_resources/read_resource tools for MCP clients",
        "      that don't support resources natively. Adds 3 extra tools.",
        "  enable_tool_search:",
        "    name: Enable tool search",
        "    description: >-",
        "      Replace the full tool catalog with search-based discovery. Reduces",
        "      idle context from ~46K to ~5K tokens. Use this if using an LLM without",
        "      deferred tools or with smaller context windows. Tools are found via",
        "      ha_search_tools and executed via categorized proxies (read/write/delete).",
        "      Requires restart to take effect.",
        "  tool_search_max_results:",
        "    name: Tool search max results",
        "    description: >-",
        "      Maximum number of tools returned by ha_search_tools when tool",
        "      search is enabled. Lower values (2-3) save context tokens but",
        "      may miss relevant tools. Range: 2-10. Requires restart.",
        "  enable_yaml_config_editing:",
        "    name: Enable YAML config editing",
        "    description: >-",
        "      Allow AI assistants to add, replace, or remove top-level keys in",
        "      configuration.yaml and packages/*.yaml. Only whitelisted keys are",
        "      allowed (e.g., template, sensor, command_line, mqtt). Core keys",
        "      like homeassistant, http, and recorder are blocked. A backup is",
        "      created before every edit. Use for YAML-only features that have no",
        "      UI or API alternative. Requires restart to take effect.",
        "  tools:",
        "    name: Advanced tool configuration",
        "    description: >-",
        "      Configure tool availability and pinning per tool. Each tool can be",
        "      enabled-unpinned, enabled-pinned (always visible in tool search),",
        "      or disabled. Some core tools (ha_search_entities, ha_get_overview,",
        "      ha_get_state, ha_report_issue) cannot be disabled. For full tool",
        "      descriptions visit https://homeassistant-ai.github.io/ha-mcp/tools",
        "      — Requires restart.",
    ]

    return "\n".join(lines) + "\n"


def update_addon_config(tools: list[dict]) -> None:
    """Update config.yaml files with generated tool sections."""
    opt_block, sch_block = generate_addon_config_tools(tools)

    for addon_dir in (ADDON_DIR, ADDON_DEV_DIR):
        config_path = addon_dir / "config.yaml"
        if not config_path.exists():
            continue
        content = config_path.read_text()

        # Replace or insert options block
        if CONFIG_TOOLS_START in content:
            content = re.sub(
                rf"{re.escape(CONFIG_TOOLS_START)}.*?{re.escape(CONFIG_TOOLS_END)}",
                opt_block, content, flags=re.DOTALL,
            )
        else:
            content = content.replace("schema:", f"{opt_block}\nschema:")

        # Replace or insert schema block
        if SCHEMA_TOOLS_START in content:
            content = re.sub(
                rf"{re.escape(SCHEMA_TOOLS_START)}.*?{re.escape(SCHEMA_TOOLS_END)}",
                sch_block, content, flags=re.DOTALL,
            )
        else:
            content = content.rstrip() + "\n" + sch_block + "\n"

        config_path.write_text(content)
        print(f"Updated {config_path.relative_to(REPO_ROOT)} tool sections")


def update_addon_translations(tools: list[dict]) -> None:
    """Write generated translations/en.yaml."""
    content = generate_addon_translations(tools)
    translations_path = ADDON_DIR / "translations" / "en.yaml"
    translations_path.write_text(content)
    print(f"Updated {translations_path.relative_to(REPO_ROOT)}")

    # addon-dev uses a symlink; if it's a regular file, overwrite it too
    dev_path = ADDON_DEV_DIR / "translations" / "en.yaml"
    if dev_path.exists() and not dev_path.is_symlink():
        dev_path.write_text(content)
        print(f"Updated {dev_path.relative_to(REPO_ROOT)}")


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

    if README_PATH.read_text() != update_readme(tools):
        print("OUT OF SYNC: README.md", file=sys.stderr)
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

        README_PATH.write_text(update_readme(tools))
        print(f"Updated {README_PATH.relative_to(REPO_ROOT)}")

        update_addon_config(tools)
        update_addon_translations(tools)


if __name__ == "__main__":
    main()
