# Developer Mode

Developer mode registers two hidden MCP tools intended for people developing
or testing ha-mcp itself. It is **off by default**, and while it is off the
tools are never registered — MCP clients cannot see or call them.

> **Warning**: with developer mode on, any connected MCP client (i.e. any AI
> agent using this server) can change server settings and replace the running
> server version. Enable it only on instances used for development/testing.

## Enabling

The toggle lives at the **very bottom of the web settings UI**: Server
Settings tab → **Developer** section (below the beta features). Flip the
switch, confirm the warning, and restart the server for the tools to
register. Alternatively set the `HAMCP_ENABLE_DEV_MODE=true` env var.

The flag is intentionally absent from the add-on Configuration page.

Rewriting tool security policies and deciding queued approvals needs a second
toggle in the same section — see [Security policy access](#security-policy-access).

## Tools

### `ha_dev_manage_server`

| Action | What it does |
| ------ | ------------- |
| `info` | Reports server version, deployment mode (embedded / add-on / standalone), Python version, data dir, HA version, and — when the [in-process server](in-process-server.md) entry exists — its current channel and pip spec. |
| `update_source` | Points the in-process (custom component) server at a release `channel` (`stable` / `dev`) or an explicit `pip_spec` — a version pin or a GitHub tarball URL such as `https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/<PR>/head.tar.gz` — then reinstalls and restarts it via the component's own options flow. |
| `restart` | Restarts this server: config-entry reload in embedded mode, Supervisor self-restart in add-on mode. Standalone processes must be restarted externally. |
| `list_pending` | Lists the tool calls currently blocked on a tool-security-policy approval, with each one's token, arguments, and expiry. Reports an empty list when policies were off at startup (no approval queue exists). |
| `approve` / `deny` | Decides one blocked call by `token`. Requires [security policy access](#security-policy-access). |

`update_source` makes PR testing a one-call operation on an in-process
server install: point the pip spec at the PR tarball, wait for the reinstall,
reconnect, and verify with `info`. No extra repos or add-on rebuilds needed.
Server-code updates apply on the entry reload itself (component >= 1.0.1
purges the module cache per worker start); a change that needs *newer
third-party dependencies* still wants a Home Assistant core restart, since
shared libraries already loaded by the HA process are not reloaded.

### `ha_dev_manage_settings`

| Action | What it does |
| ------ | ------------- |
| `list` | Returns the full server-settings matrix (the same fields as the web UI's Server Settings tab) with each value's origin: `env` (pinned, read-only), `file` (override file), `addon` (Supervisor-managed), or `default`. |
| `set` | Validates and persists one setting through the same override layer the web UI uses. Env-pinned settings are refused; beta sub-flags still require the beta master to be on. |
| `reset` | Removes one setting's override-file entry, returning it to its default. Refused for env-pinned and add-on-managed settings, like `set`. |
| `list_tools` | Returns the Tools tab payload: every tool with its state (enabled / disabled / pinned), effective LLM-API exposure, per-tool security gate, and the env-pinned / mandatory / best-practice locks that make a row read-only. |
| `set_tool` | Changes one tool's `state`, `llm_api` exposure, and/or its security `gate` (`gated=`). All requested changes are validated before anything is written. `tool='*'` is refused — author wildcard rules through `set_policy`. `gated=` requires [security policy access](#security-policy-access). |
| `get_policy` | Returns the full tool-security policy (`wait_seconds`, `approval_ttl_minutes`, `rules`, `version`, `schema_version`) plus whether the policy engine is enabled and live. |
| `set_policy` | Writes the full policy, schema-validated and guarded by the `version` from your last `get_policy` (optimistic concurrency). Requires [security policy access](#security-policy-access). |
| `get_backup_config` | Returns the auto-backup config fields (the Backups tab's settings), with each value's origin. |
| `set_backup_config` | Changes auto-backup settings, routed through the Supervisor in add-on mode and the override file elsewhere. |

Changes persist immediately but — like the web UI — most settings only take
effect after a restart (`ha_dev_manage_server` `restart`). Security gates,
policy edits, and LLM-API exposure apply live.

## Security policy access

The dev tools can rewrite the very policies that gate the agent calling them:
`set_policy` replaces the policy wholesale, `set_tool` with `gated=` adds or
removes a per-tool approval gate, and `approve` / `deny` decide queued
approvals — and because a gated call's error carries its approval token, an
agent could otherwise accept its own blocked calls.

That access is a **separate toggle, off by default**:
`dev_tools_security_policy_access` (`HAMCP_DEV_SECURITY_POLICY_ACCESS`), in
the same **Developer** section of the web settings UI, directly below the
dev-mode switch. Developer mode can stay on with policy access off.

While it is off, these are refused with `AUTH_INSUFFICIENT_PERMISSIONS`:

- `ha_dev_manage_settings` `set_policy`
- `ha_dev_manage_settings` `set_tool` with `gated=` (a `state` / `llm_api`-only
  call still works, and a combined `state` + `gated` call is refused whole,
  before the state change is written)
- `ha_dev_manage_server` `approve` and `deny`
- `ha_dev_manage_settings` `set` / `reset` of `enable_tool_security_policies`
  — turning the engine off would bypass every gate at once on the next
  restart (and the dev tools can restart the server)

Reads stay available either way: `get_policy`, `list_tools`, `list_pending`,
and the `list` settings matrix are never gated. The matrix marks the guarded
rows honestly: `enable_tool_security_policies` reports `editable: false` with
`locked_reason: policy_access_required` while access is off, and
`dev_tools_security_policy_access` and `enable_security_policy_tool` report
`editable: false` with `locked_reason: web_ui_or_env_only`. An env-pinned row
keeps its plain env story instead — the pin is the lock an operator must lift
first.

`dev_tools_security_policy_access` applies **live** — no restart needed —
while `enable_security_policy_tool` takes effect on the next restart. The dev
tools' settings surfaces never write either: `set` and `reset` of
`dev_tools_security_policy_access` are refused even while access is on, and so
are `set` and `reset` of `enable_security_policy_tool` — flipping that flag
registers `ha_manage_security_policy` (which can rewrite the policies) once
the server restarts, the same end state as unbuckling the leash. Flip either
in the web settings UI or via the env vars.

This is a leash on the dev tools' settings surfaces, **not a sandbox**. Dev
mode's `update_source` / `restart` can still replace the running server
build, and in add-on deployments `ha_manage_app` can reach the add-on's own
options and ingress. `read_only_mode` likewise stays dev-tool-writable **by
design** — developer mode is expected to be able to lift it. Where that
boundary matters, gate those tools with policy rules — or keep dev mode off;
it remains a trusted-operator feature.
