# Settings UI translations

The settings page discovers every `*.json` catalog in this directory. No Python
or JavaScript registration is required, and no packaging file needs editing —
the wheel, sdist and binary declarations all match this directory by pattern.

## Adding a language

A language ships on **all four** translated surfaces or not at all, and one
Home Assistant language code names every file:

- `src/ha_mcp/settings_ui/locales/<code>.json` (this directory)
- `custom_components/ha_mcp_tools/translations/<code>.json`
- `homeassistant-addon/translations/<code>.yaml`
- `homeassistant-addon-dev/translations/<code>.yaml`

Adding only this catalog fails `test_every_locale_ships_on_every_surface`. Also
add the new code to the locale list in `AGENTS.md` § Translations — that list is
pinned by `test_agents_md_lists_every_shipped_locale`.

**Start from a translated catalog, not from `en.json`.** English for the tool
titles and descriptions comes from the tool definitions at runtime, so `en.json`
ships `tools` and `tool_groups` empty; a copy of it is missing both sections
that this catalog is required to carry.

## Catalog sections

- `meta.native_name`: language name shown in the selector.
- `meta.dir`: `ltr` or `rtl`. Omitting it means `ltr`; any other value is
  rejected when the catalog loads.
- `messages`: interface labels, help text, notices, and runtime messages. Keys
  may be omitted — English is the per-key fallback at runtime — but see the
  share limit below before leaving a catalog half-finished.
- `tool_groups`: one entry per renderable MCP tool tag, keyed by the English
  tag. Not optional, and exact: no key more and none fewer.
- `tools`: `title` and `description` per tool, keyed by the stable MCP tool
  name. Not optional, and exact in the same way.

Keep the keys and `{placeholders}` unchanged in every section.

## What CI checks

- Every surface carries the same set of language codes.
- `tool_groups` and `tools` name exactly the renderable groups and tools — a
  tool added to the codebase turns every locale red in the PR that adds it.
- At most 5% of this catalog's `messages`, and 5% of its `tools` texts, may be
  byte-identical to English or missing outright; the component catalogs allow
  15%. A single tool whose `title` *and* `description` are both still English
  fails by name however small the share.
- The English each translation was written against is hashed in
  `tests/src/unit/locale_source_baseline.json`, so a later edit to an English
  string turns the locales red rather than leaving them silently stale. Adding a
  language does not change any English source, so no baseline regeneration is
  needed for it.
