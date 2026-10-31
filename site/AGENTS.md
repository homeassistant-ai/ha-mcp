# Site

Astro-based project website under `site/`.

## Commands

```bash
cd site
npm run dev      # Dev server with hot reload
npm run build    # Production build
npm run preview  # Preview production build locally
```

## Deployment

`.github/workflows/deploy.yml` builds the site from the `stable` tag and
deploys it to GitHub Pages after each stable release, so the published setup
instructions match the current stable version. A site change merged to
`master` goes live at the next stable release. To publish sooner, or to
publish another tag or commit, dispatch `deploy.yml` with its `ref` input;
that override lasts only until the next weekly or release-driven rebuild.

## Setup Wizard (`site/src/pages/setup.astro` + `site/src/scripts/setup-wizard/`)

`setup.astro` holds the picker markup and the page CSS; its `<script>` only imports and starts the wizard. The behaviour lives in ES modules under `site/src/scripts/setup-wizard/`, which Astro bundles with the page. They are plain JavaScript (the inline script they came from was not type-checked), and `tests/src/unit/test_module_size_ratchet.py` caps each at 1,000 lines, so a step that grows past that gets its own module.

| Module | Owns |
|---|---|
| `data.ts` | `clientsData` / `platformsData` / `scopeData` / `stdioOnlyClients` / `remoteOnlyClients`. Imported by the frontmatter (picker tiles) and by the script. |
| `state.js` | The `state` object, display labels, `needsPlatform` / `isStdioOnly` / `isRemoteOnly` / `remotePathsForMethod`, and `base` (the site base path). |
| `wizard.js` | `initSetupWizard()`: tile click handlers, section visibility and step numbering (`updateSections`), URL sync and restore. |
| `generate-config.js` | `generateConfig()`: derives the per-render context (`isStdio`, `isRemote`, `derivedDeployment`, ...), then renders badges, instructions, config, hints and notes. |
| `instructions.js` | The instruction blocks above the config box, in page order: guide link, uv / Node.js install, deployment, remote path, transport notes, stdio bridge, Gemini CLI notes. |
| `client-configs.js` | The JSON / YAML / CLI config each client gets. |
| `ui-clients.js` | Step-by-step blocks for clients configured through their own UI. |

**Adding a new client / platform / scope / server method / remote path:**

1. Add an entry to the appropriate array in `data.ts` (insert at the right `order` position). Keep each array ordered by the `order` field — the wizard renders entries in array order without re-sorting.
2. Add a branch keyed off the new entry's `id` in the module that owns it. Match neighboring patterns: JSON clients add an `else if` in `client-configs.js`; CLI clients add a CLI command emit there; UI clients add an `instruction-block` div with click steps in `ui-clients.js`; a new server method or remote path touches `remotePathsForMethod` in `state.js`, the section logic in `wizard.js`, and the deployment / remote-path blocks in `instructions.js`. A `configFormat` shared by clients with incompatible schemas needs an `id` guard of its own — see the `hermes` branch, which would otherwise inherit Continue's YAML shape. See `cursor` / `chatgpt` / `claude-code` / `cloudflared` for examples.
3. If the addition has cross-cutting troubleshooting content (PATH issues, restart requirements, version requirements), add it to `faq.astro`.

`tests/src/unit/test_astro_setup_js_behavior.py` bundles the page's `<script>` with esbuild and drives every client through JSDOM, so a branch that stops emitting its config or instructions fails there.

## Accessibility & checks

The site is accessibility-anchored (#1574/#1595). Before pushing site changes:

```bash
cd site
npm run check       # astro check — types + a11y diagnostics
npm run lint        # eslint-plugin-astro + jsx-a11y
npm run build       # required before the audit (writes dist/)
npm run audit:a11y  # axe-core over the built pages
```

CI runs these in the `site-checks` job (`.github/workflows/pr.yml`): `check`, `lint`, and the axe-core audit are all blocking. Accessibility conventions — landmarks, skip links, tablist semantics, live status regions, and the theme/contrast tier model — live in `.gemini/styleguide.md` › Accessibility.
