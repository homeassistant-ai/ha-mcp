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

A language ships on **all four** translated surfaces or not at all, with one
Home Assistant language code used across every corresponding file:

- `src/ha_mcp/settings_ui/locales/<code>.json` (this directory; authored)
- `custom_components/ha_mcp_tools/translations/<code>.json` (authored)
- `homeassistant-addon/translations/<code>.yaml` (generated)
- `homeassistant-addon-dev/translations/<code>.yaml` (generated)

<!-- Keep the following marker line intact; locale parity tests parse it. -->
The current code set `cs`, `de`, `eo`, `es`, `fr`, `it`, `ko`, `nl`, `pl`, `ru`, `sv`, `tlh`, and `zh-Hans` names every file:
the parity test keeps this documentation aligned with the shipped catalogs.

Klingon (`tlh`) is the one best-effort exception. Its catalogs may be edited
manually and still ship, but the automatic translation planner never queues
them. Missing strings use English fallback; invalid or stale Klingon catalogs
produce warnings instead of failing CI or the daily locale sync. The runtime
loader skips an invalid Klingon settings catalog, and the generator can project
English into its add-on catalogs, so Klingon cannot prevent any other locale
from loading or updating. All other language codes remain subject to every
hard gate below.

Add the two authored catalogs, then run `python scripts/generate_locales.py`
and merge: the post-merge `locale-sync.yml` workflow machine-fills every string
over its next daily runs. The component catalog may start as an empty object;
this one needs `meta` (`native_name`, `dir`) plus the handful of `messages`
keys the ungated checks below demand — a `meta`-only catalog is red in PR CI.
Two things that list will not lead you to: `policies.operators.exists_long` is
the condition editor's own dropdown label rather than a `PredicateOp` member,
so no check asks for it and a catalog without it reads English there until the
sync fills it; and because each surface samples its own catalog for the address
register the engine imitates, a component catalog left at a key or two rests
entirely on whichever of them addresses the reader —
`test_every_shipped_component_catalog_gets_reader_addressing_samples` pins that
one. Leaving that catalog empty is fine, but the moment you author anything in
it, at least two keys must be ones whose English addresses the reader in the
second person, and each must carry a non-empty translation — a key left blank is
skipped like a missing one rather than sampled empty. Most component strings do
not address the reader, so starting at the top of the file leaves the catalog
anchorless and that check red until you add ones that do. Two rather than one
because the run likeliest to need the register is the one that rewords such a
key: that queues it, and a queued key is dropped from the candidates, so a
surface resting on a single anchor loses its register in exactly that run —
`test_component_samples_survive_their_own_anchor_being_queued` pins the
survival. To fill them in your own PR instead, run
`scripts/translate_locales.py` yourself and review its output like any
other diff. Also add the new code to the current code set near the top of this
guide; `test_locale_readme_lists_every_shipped_locale` pins that list to the
shipped catalogs. The engine reads the target
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
  share limit below before leaving a catalog half-finished. Omitting is the
  only way to say "not translated yet": a key that is present but blank is
  rejected when the catalog loads, because the runtime resolves by key
  presence, so an empty value would win over English and render as nothing.
- `tool_groups`: one entry per renderable MCP tool tag, keyed by the English
  tag. Not optional, and exact: no key more and none fewer. Blank is rejected
  here too, but dropping the key is not the escape hatch it is for `messages` —
  the exact key set forbids that. A heading you have not translated yet keeps
  the English tag as its value.
- `tools`: `title` and `description` per tool, keyed by the stable MCP tool
  name. The key set is not optional and exact in the same way; either field on
  its own may be left out, but a missing one counts as untranslated against the
  share limit below. Blank is rejected here too, for the same reason.

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

In PR CI (ungated — `tests/src/unit/test_locale_parity.py` unless another file
is named):

- Every surface carries the same set of language codes.
- Settings UI `messages` may omit keys because English is the per-key
  fallback, but a locale may not carry a key absent from `en.json`: nothing
  can render it. `tool_groups` and `tools` must instead match the renderable
  keys exactly.
- Every decided `Decision` outcome (all but `pending`) and every
  `PredicateOp` operator has a word in every catalog, non-blank — and in every
  catalog but `en.json` not still spelled the way the backend does
  (`test_every_decided_outcome_has_a_catalog_word`,
  `test_every_predicate_operator_has_a_catalog_word` in
  `tests/src/unit/test_settings_ui_i18n.py`). These words render inside
  otherwise translated sentences, and the page payload merges English
  underneath, so a missing key shows English's own word rather than reading as
  a gap — and the bare enum literal where English lacks the key too. That is
  why they are owed at once rather than left to the sync.
- `policies.pending.already_decided`, the sentence one of those `Decision`
  words is interpolated into. `TestAlreadyDecidedCopy` in
  `tests/src/unit/test_settings_ui_js_behavior.py` drives the real 409 handler
  under every non-English catalog and compares the whole rendered alert, so
  carrying the word without its host sentence fails on the missing key. Whole
  sentence rather than containment is deliberate: a host that falls back to
  English still reads as an English clause around a translated word.
  **These JS behaviour tests skip unless `tests/js/` has its npm dependencies
  installed** (`npm install` there) — locally they are silent, in CI they are
  not.
- At least one translated key whose English addresses the reader in the
  second person, so `scripts/translate_locales.py` can show the engine
  how this catalog addresses its reader
  (`test_every_shipped_catalog_gets_reader_addressing_samples` in
  `tests/src/unit/test_translate_locales.py`). Without one the pipeline
  translates the rest of the catalog with no register to imitate.
- The generated files (both add-on YAMLs, `FEATURE_META`) are byte-exact
  generator output (`test_derived_catalogs_match_the_canonical_store`); run
  `python scripts/generate_locales.py` after touching any `addon.*`,
  `addon_stable.*` or `features.*` key.
- Component-catalog `{placeholder}` parity, for keys whose English still
  matches the baseline — a hand edit that drops a placeholder fails the PR
  that makes it; a translation awaiting a machine rewrite is excluded.

In the post-merge `locale-sync.yml` workflow only (the same files, gated behind
`LOCALE_COMPLETENESS_CHECKS=1` — a PR that changes English merges without
these, and the daily sync owes them afterwards):

- `tool_groups` and `tools` name exactly the renderable groups and tools.
  The check parses the registered tool set from source rather than trusting
  generated `tools.json`, so a broken generator cannot validate its own stale
  output.
- At most 5% of this catalog's `messages`, and 5% of its `tools` texts, may be
  byte-identical to English or missing outright. The 5% ceiling also applies
  independently to each generated app projection computed from the canonical
  store. Component catalogs allow 15%, because they carry product names as
  keys of their own. A single tool
  whose `title` *and* `description` are both still English fails by name
  however small the share.
- The English each translation was written against is hashed in
  `tests/src/unit/locale_source_baseline.json`, so a later edit to an English
  string reads as stale rather than silently keeping the old meaning —
  `scripts/translate_locales.py` retranslates exactly those keys and repins
  the baseline. Adding a language does not change any English source, so no
  baseline regeneration is needed for it.
`test_locale_sync_gate_shape.py` pins this gated workflow wiring. A successful
`locale-sync.yml` run pushes the regenerated catalogs straight to `master`
with the release App credential and can include everything merged since the
previous run.

## English changes and retranslation

An English change is a one-place edit:

- Settings messages: `en.json` `messages`.
- Tool title or summary: the tool definition; English `tools` is intentionally
  empty in `en.json`.
- Component config flow: `strings.json` plus component `en.json`.

After a canonical app-option or feature string changes, run
`python scripts/generate_locales.py` so the generated projections match. A
pull request does not owe machine translations. The daily post-merge
`locale-sync.yml` compares English-source hashes in
`tests/src/unit/locale_source_baseline.json`, translates changed or missing
keys through the configured Gemini-compatible endpoint, validates placeholders
and markup, regenerates projections, and repins the baseline.

To supply a human translation in the same pull request, edit the authored
catalogs and run `python scripts/update_locale_baseline.py`. The baseline is
what tells the next sync that the translation covers the current English;
without that repin, the sync will correctly treat the value as stale and
replace it. Running `scripts/translate_locales.py` locally also repins after
its machine-generated pass.

A locale pull request that remained open while its English source changed needs
special review. Compare its affected values with that surface's current English
before merging. An old translated value can still have the right key and a
translated-looking value, so ordinary parity and untranslated-share checks
cannot detect the stale meaning. Repinning is not a repair because it blesses
the stale value; delete or update the value so the planner queues the correct
work. Issue #1993 is the precedent: an English policy changed from ALL-match
to ANY-match while one locale still asserted the opposite. Numbers and
code-like identifiers are the cheap signal because rewording often moves one;
`test_translations_keep_english_numbers_and_identifiers` checks them across
all three authored surfaces, but cannot replace semantic review.

Tool docstring summaries are English translation sources. Editing a summary
queues its translated title/description in every locale. `Field(description=)`
text is not in this baseline. For a feature-gated tool, the user-facing
`FEATURE_GATED_TOOLS` stub is the translation source; changing only the
parsed docstring holds that key stale until a human confirms the stub remains
accurate and runs `python scripts/update_locale_baseline.py`.

## Sync failures and recovery

The translation workflow uses conservative pacing and retries transient 429,
5xx, and timeout failures with backoff. A repeatedly failing request records
its strings as failed and continues; two dead batches stop the run before it
burns the remaining quota.

A partial run commits completed translations plus
`tests/src/unit/locale_sync_progress.json`. Rerun the workflow or wait for the
next daily run; it resumes from that file. Only a fully successful run repins
the baseline and removes progress, so incomplete translation work remains
visible as a red sync.

When the engine is unavailable, a human may translate the dry-run list, run
`python scripts/generate_locales.py` and
`python scripts/update_locale_baseline.py`, and open a normal pull request.
Human edits win because the machine touches only missing or stale strings. The
provider boundary is `_call_gemini`, configured by `GEMINI_API_URL`,
`GEMINI_MODEL`, and `GEMINI_API_KEY`.

The Webhook Proxy app and its bundled integration remain English-only by
decision. Their tests intentionally reject an accidental partial catalog.
Any other new catalog directory fails until it is translated across every
required surface or explicitly added to the English-only decision.
