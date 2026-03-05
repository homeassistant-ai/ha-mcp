"""Validate MCP server manifest against the published JSON schema."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from jsonschema import validate

SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-10-17/server.schema.json"


def fetch_schema(url: str) -> dict:
    """Fetch the JSON schema from the provided URL."""
    with urllib.request.urlopen(url) as response:  # type: ignore[arg-type]
        return json.load(response)


def load_manifest(path: Path) -> dict:
    """Load a local JSON manifest file."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to the server manifest JSON file")
    parser.add_argument(
        "--schema-url",
        default=SCHEMA_URL,
        help="URL for the MCP server manifest JSON schema (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    schema = fetch_schema(args.schema_url)
    manifest = load_manifest(args.manifest)

    validate(instance=manifest, schema=schema)
    print("server.json schema validation succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
