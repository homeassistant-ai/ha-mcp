#!/usr/bin/env python3
"""Generate mcpb manifest.json with auto-discovered tools from the codebase."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path


def extract_tools_from_file(file_path: Path) -> list[dict]:
    """Extract tool definitions from a Python file."""
    tools = []
    content = file_path.read_text(encoding="utf-8")
    tree = ast.parse(content)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("ha_"):
            # Get the docstring
            docstring = ast.get_docstring(node) or ""
            # Take first line as description
            description = docstring.split("\n")[0].strip() if docstring else ""

            # If no docstring, try to get from decorator
            if not description:
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        for keyword in decorator.keywords:
                            if keyword.arg == "description" and isinstance(keyword.value, ast.Constant):
                                description = keyword.value.value
                                break
                            if keyword.arg == "annotations" and isinstance(keyword.value, ast.Dict):
                                for k, v in zip(keyword.value.keys, keyword.value.values):
                                    if isinstance(k, ast.Constant) and k.value == "title":
                                        if isinstance(v, ast.Constant):
                                            description = v.value
                                            break

            # Fallback to function name conversion
            if not description:
                description = node.name.replace("ha_", "").replace("_", " ").title()

            tools.append({
                "name": node.name,
                "description": description[:100]  # Truncate long descriptions
            })

    return tools


def discover_all_tools(tools_dir: Path) -> list[dict]:
    """Discover all tools from the tools directory."""
    all_tools = []

    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        tools = extract_tools_from_file(py_file)
        all_tools.extend(tools)

    # Sort by name for consistency
    all_tools.sort(key=lambda t: t["name"])
    return all_tools


def generate_manifest(
    template_path: Path,
    output_path: Path,
    version: str,
    platform: str,
    binary_ext: str,
    tools: list[dict]
):
    """Generate manifest.json from template with discovered tools."""
    template = json.loads(template_path.read_text(encoding="utf-8"))

    # Update tools list
    template["tools"] = tools
    template["tools_generated"] = True

    # Replace placeholders
    manifest_str = json.dumps(template, indent=2)
    manifest_str = manifest_str.replace("${VERSION}", version)
    manifest_str = manifest_str.replace("${PLATFORM}", platform)
    manifest_str = manifest_str.replace("${BINARY_EXT}", binary_ext)

    # Update description with actual tool count
    manifest = json.loads(manifest_str)
    manifest["long_description"] = manifest["long_description"].replace(
        "80+ specialized tools",
        f"{len(tools)} specialized tools"
    )

    output_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Generated manifest with {len(tools)} tools -> {output_path}")


def main():
    if len(sys.argv) < 4:
        print("Usage: generate_manifest.py <version> <platform> <binary_ext>")
        print("Example: generate_manifest.py 4.7.4 win32 .exe")
        sys.exit(1)

    version = sys.argv[1]
    platform = sys.argv[2]
    binary_ext = sys.argv[3] if len(sys.argv) > 3 else ""

    # Paths - script is in dist/mcpb/, project root is 2 levels up
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    tools_dir = project_root / "src" / "ha_mcp" / "tools"
    template_path = script_dir / "manifest.template.json"
    output_path = project_root / "mcpb-bundle" / "manifest.json"

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Discover tools
    tools = discover_all_tools(tools_dir)

    # Generate manifest
    generate_manifest(template_path, output_path, version, platform, binary_ext, tools)


if __name__ == "__main__":
    main()
