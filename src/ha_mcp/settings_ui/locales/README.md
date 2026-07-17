# Settings UI translations

The settings page discovers every `*.json` catalog in this directory. To add a
language, copy `en.json`, rename it to the language code (for example
`it.json`), translate the values, and keep the keys and `{placeholders}`
unchanged. No Python or JavaScript registration is required.

Catalog sections:

- `meta.native_name`: language name shown in the selector.
- `meta.dir`: `ltr` or `rtl`.
- `messages`: interface labels, help text, notices, and runtime messages.
- `tool_groups`: optional translations keyed by the English MCP tool tag.
- `tools`: optional per-tool `title` and `description` overrides keyed by the
  stable MCP tool name. Missing values fall back to the server-provided English.

English is always the fallback, so an incomplete catalog remains usable while
it is being expanded.
