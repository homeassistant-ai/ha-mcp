"""Render the MCP server manifest with release metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("server.json"),
        help="Path to the template server manifest (default: server.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Destination for the rendered manifest (default: overwrite input)",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Release version to inject into the manifest",
    )
    parser.add_argument(
        "--oci-image",
        required=True,
        help="Fully-qualified OCI image reference for this release",
    )
    return parser.parse_args()


def replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for placeholder, replacement in replacements.items():
            result = result.replace(placeholder, replacement)
        return result
    if isinstance(value, list):
        return [replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_placeholders(item, replacements) for key, item in value.items()}
    return value


def main() -> int:
    args = parse_args()
    output_path = args.output or args.input

    manifest_data = json.loads(args.input.read_text(encoding="utf-8"))
    replacements = {
        "{{VERSION}}": args.version,
        "{{OCI_IMAGE}}": args.oci_image,
    }
    rendered = replace_placeholders(manifest_data, replacements)
    rendered_json = json.dumps(rendered, indent=2)

    if "{{" in rendered_json:
        unresolved = sorted({
            fragment.split("}}", maxsplit=1)[0] + "}}"
            for fragment in rendered_json.split("{{")[1:]
        })
        raise ValueError(f"Unresolved manifest placeholders: {', '.join(unresolved)}")

    output_path.write_text(rendered_json + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
