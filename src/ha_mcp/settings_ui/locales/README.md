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
add the new code to the locale list in the repository-root `AGENTS.md`
§ Translations — that list is pinned by
`test_agents_md_lists_every_shipped_locale`.

Only the first of those four files is described below, and the other three
carry content rules of their own: the component catalog needs every `en.json`
key with identical `{placeholders}` and no extra ones, and each add-on YAML
needs a `name` and a `description` for every `schema:` key of *that* flavor's
`config.yaml`, with nothing left behind for a key the schema no longer
declares. The two flavors declare different schemas, so neither YAML is a copy
of the other. The repository-root `AGENTS.md` § Translations states all of
this; a contributor who writes only this catalog goes red on the other three.

**Start from a translated catalog, not from `en.json`.** English for the tool
titles and descriptions comes from the tool definitions at runtime, so `en.json`
ships `tools` and `tool_groups` empty; a copy of it is missing both sections
that this catalog is required to carry.

**Read the other surfaces before you word a switch.** Wherever the same English
text is shipped from more than one catalog, your wording has to be byte-identical
in all of them. That is not only the add-on-options-versus-settings-UI axis: the
two add-on flavors describe most of the same options, so a good part of the
pinned parity is stable-against-dev, with no settings-UI text involved at all.
Translating one surface at a time is exactly how one option ends up with two
different sentences.

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

- Every surface carries the same set of language codes.
- `tool_groups` and `tools` name exactly the renderable groups and tools — a
  tool added to the codebase turns every locale red in the PR that adds it.
- At most 5% of this catalog's `messages`, and 5% of its `tools` texts, may be
  byte-identical to English or missing outright. Both add-on flavors are held to
  the same 5%; the component catalogs allow 15%, because they carry product
  names as keys of their own. A single tool whose `title` *and* `description` are
  both still English fails by name however small the share.
- One wording per English string across surfaces, wherever the same English text
  is shipped from more than one catalog. The failure names every group that
  disagrees. Where a group has a settings UI member, that is the wording the
  other surfaces follow today; a group carried only by the two add-on flavors
  has no such member, so there pick one wording and use it in both.
- The English each translation was written against is hashed in
  `tests/src/unit/locale_source_baseline.json`, so a later edit to an English
  string turns the locales red rather than leaving them silently stale. Adding a
  language does not change any English source, so no baseline regeneration is
  needed for it.
