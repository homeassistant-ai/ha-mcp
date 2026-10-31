# tools.json

Auto-generated MCP tool catalog. **Do not edit manually.** The `sync-tool-docs.yml` workflow regenerates it with `python scripts/extract_tools.py` on every push to `master` that touches tool sources and commits the result; run the script by hand only to preview a change locally.

Each entry contains: `name`, `title`, `description` (full docstring), `inputSchema` (AST-extracted params with types/descriptions/defaults), `annotations` (readOnlyHint, destructiveHint, etc.), `tags` (category), `source_file`.

Consumed by the Astro Tool Explorer page (`site/src/pages/tools.astro`).
The same script also generates the README.md tool table and the tools section of `homeassistant-addon/DOCS.md`.
