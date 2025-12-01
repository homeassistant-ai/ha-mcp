#!/usr/bin/env python3
"""Generate mcpb manifest.json with auto-discovered tools from the codebase."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def extract_annotations_from_decorator(decorator: ast.Call) -> dict:
    """Extract annotations dict from @mcp.tool(annotations={...}) decorator."""
    annotations = {}
    for keyword in decorator.keywords:
        if keyword.arg == "annotations" and isinstance(keyword.value, ast.Dict):
            for k, v in zip(keyword.value.keys, keyword.value.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    annotations[k.value] = v.value
    return annotations


def infer_annotations_from_name(tool_name: str) -> dict:
    """Infer annotations based on tool name patterns."""
    name_lower = tool_name.lower()

    # Read-only patterns
    read_only_prefixes = ("ha_get_", "ha_list_", "ha_search_", "ha_find_")
    read_only_contains = ("_history", "_statistics", "_logbook", "_traces", "_status")

    # Destructive patterns
    destructive_prefixes = ("ha_delete_", "ha_remove_")
    destructive_contains = ("_delete", "_remove")

    if any(name_lower.startswith(p) for p in read_only_prefixes):
        return {"readOnlyHint": True}
    if any(c in name_lower for c in read_only_contains):
        return {"readOnlyHint": True}
    if any(name_lower.startswith(p) for p in destructive_prefixes):
        return {"destructiveHint": True}
    if any(c in name_lower for c in destructive_contains):
        return {"destructiveHint": True}

    # Default: assume it can modify state
    return {}


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

            # Extract annotations from decorator
            annotations = {}

            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    # Extract annotations from @mcp.tool(annotations={...})
                    annotations = extract_annotations_from_decorator(decorator)

                    # If no docstring, try to get description from decorator
                    if not description:
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

            # Fallback to function name conversion for description
            if not description:
                description = node.name.replace("ha_", "").replace("_", " ").title()

            # If no annotations found, infer from tool name
            if not annotations or ("readOnlyHint" not in annotations and "destructiveHint" not in annotations):
                inferred = infer_annotations_from_name(node.name)
                annotations = {**inferred, **annotations}  # Keep explicit annotations, add inferred

            tool = {
                "name": node.name,
                "description": description[:100]  # Truncate long descriptions
            }

            # Only include annotations that are relevant for MCPB
            mcpb_annotations = {}
            if annotations.get("readOnlyHint"):
                mcpb_annotations["readOnlyHint"] = True
            if annotations.get("destructiveHint"):
                mcpb_annotations["destructiveHint"] = True

            if mcpb_annotations:
                tool["annotations"] = mcpb_annotations

            tools.append(tool)

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

    # Print stats
    read_only = sum(1 for t in tools if t.get("annotations", {}).get("readOnlyHint"))
    destructive = sum(1 for t in tools if t.get("annotations", {}).get("destructiveHint"))
    other = len(tools) - read_only - destructive
    print(f"Generated manifest with {len(tools)} tools -> {output_path}")
    print(f"  - {read_only} read-only tools")
    print(f"  - {destructive} destructive tools")
    print(f"  - {other} other tools (can modify state)")


def main():
    if len(sys.argv) < 4:
        print("Usage: generate_manifest.py <version> <platform> <binary_ext>")
        print("Example: generate_manifest.py 4.7.4 win32 .exe")
        sys.exit(1)

    version = sys.argv[1]
    platform = sys.argv[2]
    binary_ext = sys.argv[3] if len(sys.argv) > 3 else ""

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
    generate_manifest(template_path, output_path, version, platform, binary_ext, tools)


if __name__ == "__main__":
    main()
