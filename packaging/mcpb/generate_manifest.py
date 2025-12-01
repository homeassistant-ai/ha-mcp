#!/usr/bin/env python3
"""Generate mcpb manifest.json with auto-discovered tools from the codebase."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def extract_tools_from_file(file_path: Path) -> list[dict]:
    """Extract tool definitions from a Python file.

    MCPB manifest only supports 'name' and 'description' for tools.
    We use the 'title' from annotations as the display name.
    """
    tools = []
    content = file_path.read_text(encoding="utf-8")
    tree = ast.parse(content)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("ha_"):
            # Get the docstring for description
            docstring = ast.get_docstring(node) or ""
            description = docstring.split("\n")[0].strip() if docstring else ""

            # Try to get title from decorator annotations
            title = None
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if keyword.arg == "annotations" and isinstance(keyword.value, ast.Dict):
                            for k, v in zip(keyword.value.keys, keyword.value.values):
                                if isinstance(k, ast.Constant) and k.value == "title":
                                    if isinstance(v, ast.Constant):
                                        title = v.value
                                        break
                        # Also check for description in decorator if no docstring
                        if not description and keyword.arg == "description" and isinstance(keyword.value, ast.Constant):
                            description = keyword.value.value

            # Use title as the display name, fallback to formatted function name
            display_name = title if title else node.name.replace("ha_", "").replace("_", " ").title()

            # Use docstring first line as description, fallback to title or formatted name
            if not description:
                description = display_name

            # MCPB only supports name and description
            # Use title/display_name as the "name" shown in UI
            tools.append({
                "name": display_name,
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
    tools: list[dict]
):
    """Generate manifest.json from template with discovered tools.

    Creates a multi-platform bundle supporting both macOS and Windows.
    """
    template = json.loads(template_path.read_text(encoding="utf-8"))

    # Update tools list
    template["tools"] = tools
    template["tools_generated"] = True

    # Replace version placeholder
    manifest_str = json.dumps(template, indent=2)
    manifest_str = manifest_str.replace("${VERSION}", version)

    # Update description with actual tool count
    manifest = json.loads(manifest_str)
    manifest["long_description"] = manifest["long_description"].replace(
        "80+ specialized tools",
        f"{len(tools)} specialized tools"
    )

    output_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Generated manifest with {len(tools)} tools -> {output_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: generate_manifest.py <version>")
        print("Example: generate_manifest.py 4.7.4")
        sys.exit(1)

    version = sys.argv[1]

    # Paths - script is in packaging/mcpb/, project root is 2 levels up
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
    generate_manifest(template_path, output_path, version, tools)


if __name__ == "__main__":
    main()
