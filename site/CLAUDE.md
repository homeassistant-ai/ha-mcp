# Site

Astro-based project website under `site/`.

## Commands

```bash
cd site
npm run dev      # Dev server with hot reload
npm run build    # Production build
npm run preview  # Preview production build locally
```

## Setup Wizard (`site/src/pages/setup.astro`)

Single-file Astro page that drives the on-site setup flow. Both the metadata (which clients/platforms/connections/deployments exist) and the per-client instruction prose live in this one file.

**Data** — four pre-sorted JS arrays at the top of the component frontmatter:

```ts
const clientsData = [...]    // 19 supported AI clients
const platformsData = [...]  // macOS / Linux / Windows / Docker
const connectionsData = [...]// local / network / remote
const deploymentData = [...] // uvx / docker / ha-addon / cloudflared / webhook-proxy
```

These feed the picker tiles in the markup section AND the wizard `<script>` block (`state.client`, `state.connection`, etc.).

**Instruction templates** are JS template literals inside the `<script>` block, keyed off `state.client.id` / `platformId` / `state.connection.id` / `state.proxy`. Cross-cutting troubleshooting and restart-related help lives in `site/src/pages/faq.astro`; OS-specific install walkthroughs live in `guide-macos.astro` / `guide-windows.astro`.

**Adding a new client / platform / connection / deployment:**

1. Add an entry to the appropriate inline array (insert at the right `order` position). Keep each array ordered by the `order` field — the wizard renders entries in array order without re-sorting.
2. Add a wizard branch in the `<script>` block keyed off the new entry's `id`. Match neighboring patterns: JSON clients add an `else if` in the JSON config builder; CLI clients add a CLI command emit; UI clients add an `instruction-block` div with click steps. See `cursor` / `chatgpt` / `claude-code` / `cloudflared` for examples.
3. If the addition has cross-cutting troubleshooting content (PATH issues, restart requirements, version requirements), add it to `faq.astro`.
