# Webhook Proxy Add-on (stable + dev)

## What it is
A thin add-on that does NOT run an MCP server. It discovers a running ha-mcp server
add-on (stable `ha_mcp`, then dev `ha_mcp_dev`), installs a custom component into HA
Core, registers a webhook (`/api/webhook/<id>`) plus optional OAuth 2.1 views, and
proxies remote MCP traffic to the server add-on's local port. Built locally by
Supervisor from the Dockerfile (no prebuilt image) — a "release" is a `version` bump in
`config.yaml` on the branch the add-on store points at.

## Two flavors
- Stable: dir `homeassistant-addon-webhook-proxy/`, slug `ha_mcp_webhook_proxy`,
  component domain `mcp_proxy`, `boot: auto`.
- Dev: dir `homeassistant-addon-webhook-proxy-dev/`, slug `ha_mcp_webhook_proxy_dev`,
  component domain `mcp_proxy_dev`, `boot: manual`, `stage: experimental`.

They are a **hand-maintained duplicate** — no codegen. The dev tree is the stable tree
with every `mcp_proxy` token rewritten to `mcp_proxy_dev` (this also renames the
`/config/.mcp_proxy_*` state files, `/opt/mcp_proxy`, the `/api/mcp_proxy/oauth` base,
the HA config-entry domain, and `/config/custom_components/mcp_proxy`). `/data/*` files
(webhook id, OAuth creds) are already isolated per add-on. CI test
`tests/src/unit/test_webhook_proxy_dev_isolation.py` fails if any bare `mcp_proxy` token
leaks into the dev tree.

## Dev-first, promote-only
**A PR never edits the stable tree directly in regular operation.** The
`webhook-proxy-stable-guard` workflow blocks any PR touching
`homeassistant-addon-webhook-proxy/` unless it comes from a `promote-webhook-proxy/*`
branch or carries the `allow-stable-edit` label (stable-only hotfixes). Every change
(code *and* docs) lands on the dev flavor first — with the version bump
`webhook-proxy-dev-version-guard` enforces — gets tested on the dev channel, and
reaches stable through the promote workflow (see Promotion below).

The only exception is this file (and its `CLAUDE.md` symlink): it is the contributor
doc for both flavors (the dev tree carries only a pointer stub to it), the promote
transform never touches it, and the guard exempts it — edit it directly in a normal
PR. The exemption is pinned to exactly these two paths in their doc shape (regular
file + symlink to it); anything else at or under those names stays guarded. `DOCS.md`
is part of the transform (token rename + flavor-banner swap) and follows the dev-first
flow like code; `CHANGELOG.md` stays per-flavor and gets a manual entry on each
promote PR.

## Mutual exclusion
Both flavors install a webhook + OAuth views; the OAuth provider owns the root
`/authorize` and `/token` routes, which two live integrations cannot share. So only one
flavor may run at a time. Each `start.py` checks the Supervisor `/addons` list on
startup and refuses (logs + a self-clearing HA notification, then exits) if its sibling
is `started`. `start.py:_sibling_is_running` matches the sibling by exact slug or
`_<base>` suffix (Supervisor hash-prefixes third-party slugs).

## Versioning
- Dev: bump `homeassistant-addon-webhook-proxy-dev/config.yaml` `version` AND
  `mcp_proxy_dev/manifest.json` `version` together (they must stay equal). The
  `webhook-proxy-dev-version-guard` workflow fails any PR that touches the dev add-on
  without an increase. Use the `Webhook Proxy Dev — Bump Version` workflow
  (`workflow_dispatch`, never scheduled) to do the bump and open a draft PR, or edit the
  two files by hand.
- Stable keeps its own independent version line and never inherits a `.devN` label.

## Promotion (dev -> stable)
When the dev flavor is ready to become stable, run the `Webhook Proxy — Promote Dev to
Stable` workflow (`workflow_dispatch`): it runs `scripts/webhook_proxy_sync.py
--direction promote`, verifies the result with the drift guards
(`tests/src/unit/test_webhook_proxy_sync.py`), bumps stable's own version, and opens a
draft promote PR. The transform also carries `DOCS.md` across (component-token
rename plus the flavor-banner swap; the canonical banners live in `DOCS_BANNERS`
in the sync script, kept honest by `test_docs_banners_match_canonical`).
`AGENTS.md` and `CHANGELOG.md` are left untouched — review them by hand on that
PR (`test_stable_docs_free_of_dev_identity` backstops the banner swap so dev
framing can never ship to stable users). What the transform does (also the
manual fallback):
1. Copy the changed dev files onto the stable dir.
2. Reverse-rename `mcp_proxy_dev` -> `mcp_proxy` everywhere (the inverse of the dev
   transform): component dir `mcp_proxy_dev/` -> `mcp_proxy/`, `DOMAIN`, `/opt` path,
   `/config/.mcp_proxy_dev_*` state files, `/api/mcp_proxy_dev/oauth` base, config-entry
   domain, and the add-on slug `ha_mcp_webhook_proxy_dev` -> `ha_mcp_webhook_proxy`.
3. In `start.py`, the stable mutual-exclusion constants are `SIBLING_SLUG_BASE =
   "ha_mcp_webhook_proxy_dev"`, `MUTEX_NOTIFICATION_ID = "mcp_proxy_mutex"`,
   `SIBLING_LABEL = "Webhook Proxy (Dev)"` (do not copy the dev-side values).
4. Set `boot: auto`, drop `stage: experimental`, remove the `(Dev)` display-name
   suffixes.
5. Bump stable's own `config.yaml` + `manifest.json` version (its independent line).

## Testing
`tests/addon/test_webhook_proxy.py` is parametrized over BOTH flavors — an autouse
`_webhook_proxy_variant` fixture rebinds `PROXY_ADDON_DIR`/`CURRENT`, so every test runs
once as `[stable]` and once as `[dev]`. CI runs `tests/addon/`, so the dev code is
exercised on every PR — that is what makes it safe to develop on the dev flavor before
promoting. When you add a variant-specific value, add it to the `WEBHOOK_PROXY_VARIANTS`
table rather than hard-coding it in a test. `tests/src/unit/test_webhook_proxy_dev_isolation.py`
separately guards the rename (no bare `mcp_proxy` token in the dev tree) and the dev-side
mutual-exclusion constants.
