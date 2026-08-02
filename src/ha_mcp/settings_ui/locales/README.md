# Settings UI translations

The settings page discovers every `*.json` catalog in this directory. No Python
or JavaScript registration is required, and no packaging file needs editing —
the wheel, sdist and binary declarations all match this directory by pattern.

**This directory is the canonical translation store.** Besides the settings
UI's own strings, each catalog carries the add-on option strings under
`addon.<key>.*` (with `features.<key>.*` for options the settings UI also
shows, and `addon_stable.<key>.*` for flavor-specific wording). Both add-on
flavors' `translations/*.yaml` and the `FEATURE_META` block in `settings.js`
are generated from these catalogs by `scripts/generate_locales.py` — never
edit those by hand. Wherever one English string reaches the reader from more
than one surface, the translation is stored once here and projected, so
cross-surface wording cannot drift.

## Adding a language

A language ships on **all four** translated surfaces or not at all, and one
Home Assistant language code names every file:

- `src/ha_mcp/settings_ui/locales/<code>.json` (this directory; authored)
- `custom_components/ha_mcp_tools/translations/<code>.json` (authored)
- `homeassistant-addon/translations/<code>.yaml` (generated)
- `homeassistant-addon-dev/translations/<code>.yaml` (generated)

Add the two authored catalogs — this one may start as a `meta`-only stub
(`native_name`, `dir`) — then run `python scripts/generate_locales.py` and
merge: the post-merge `locale-sync.yml` workflow machine-fills every string
over its next daily runs. To fill them in your own PR instead, run
`scripts/translate_locales.py` yourself and review its output like any
other diff. Also add the new code to the locale list in the repository-root
`AGENTS.md`
§ Translations — that list is pinned by
`test_agents_md_lists_every_shipped_locale`. The engine reads the target
language from `meta.native_name`, so any language an LLM can write — natural
or constructed — needs no pipeline change.

## Catalog sections

- `meta.native_name`: language name shown in the selector. It must be
  non-empty, must not repeat English's own name, and must differ from every
  other catalog's — a copied catalog that keeps the name it was copied from
  fails `test_native_names_name_their_own_language`, because the picker would
  then offer one label twice.
- `meta.dir`: `ltr` or `rtl`. Omitting it means `ltr`; any other value is
  rejected when the catalog loads.
- `messages`: interface labels, help text, notices, and runtime messages. Keys
  may be omitted — English is the per-key fallback at runtime — but see the
  share limit below before leaving a catalog half-finished.
- `tool_groups`: one entry per renderable MCP tool tag, keyed by the English
  tag. Not optional, and exact: no key more and none fewer.
- `tools`: `title` and `description` per tool, keyed by the stable MCP tool
  name. The key set is not optional and exact in the same way; either field on
  its own may be left out, but a missing one counts as untranslated against the
  share limit below.

Keep the keys and `{placeholders}` unchanged in every section.

`messages` values carry two further rules, both enforced when the catalog
loads rather than by a named test — breaking one raises a `ValueError` at
import, so the failure names the file but arrives as a broken test module:

- The only inline markup the page can restore is `<code>`, `<strong>`, `</a>`
  and `<a href="#" data-panel-link="...">`, spelled exactly that way. Any other
  tag — `<b>`, `<CODE>`, `<code >` — is rejected.
- A `data-panel-link` target must be a tab the settings page declares, and a
  translated message must link to the same tabs as its English source, with
  the same multiplicity. The order may differ, so a translation is free to
  reorder two links to suit its grammar.

## What CI checks

In PR CI (`tests/src/unit/test_locale_parity.py`, ungated):

- Every surface carries the same set of language codes.
- The generated files (both add-on YAMLs, `FEATURE_META`) are byte-exact
  generator output (`test_derived_catalogs_match_the_canonical_store`); run
  `python scripts/generate_locales.py` after touching any `addon.*`,
  `addon_stable.*` or `features.*` key.
- Component-catalog `{placeholder}` parity, for keys whose English still
  matches the baseline — a hand edit that drops a placeholder fails the PR
  that makes it; a translation awaiting a machine rewrite is excluded.

In the post-merge `locale-sync.yml` workflow only (the same test file, gated
behind `LOCALE_COMPLETENESS_CHECKS=1` — a PR that changes English merges
without these, and the daily sync owes them afterwards):

- `tool_groups` and `tools` name exactly the renderable groups and tools.
- At most 5% of this catalog's `messages`, and 5% of its `tools` texts, may be
  byte-identical to English or missing outright; the component catalogs allow
  15%, because they carry product names as keys of their own. A single tool
  whose `title` *and* `description` are both still English fails by name
  however small the share.
- The English each translation was written against is hashed in
  `tests/src/unit/locale_source_baseline.json`, so a later edit to an English
  string reads as stale rather than silently keeping the old meaning —
  `scripts/translate_locales.py` retranslates exactly those keys and repins
  the baseline. Adding a language does not change any English source, so no
  baseline regeneration is needed for it.
