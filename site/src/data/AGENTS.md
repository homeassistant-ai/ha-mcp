# tools.json

Auto-generated MCP tool catalog. **Do not edit manually** - regenerate with `python scripts/extract_tools.py`.

Each entry contains: `name`, `title`, `description` (full docstring), `inputSchema` (AST-extracted params with types/descriptions/defaults), `annotations` (readOnlyHint, destructiveHint, etc.), `tags` (category), `source_file`.

Consumed by the Astro Tool Explorer page (`site/src/pages/tools.astro`).
The README.md tool table is also generated from the same script.

To update after adding/changing tools: `python scripts/extract_tools.py`
