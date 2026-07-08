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

## Tools

### `ha_dev_manage_server`

| Action | What it does |
| ------ | ------------- |
| `info` | Reports server version, deployment mode (embedded / add-on / standalone), Python version, data dir, HA version, and — when the [in-process server](in-process-server.md) entry exists — its current channel and pip spec. |
| `update_source` | Points the in-process (custom component) server at a release `channel` (`stable` / `dev`) or an explicit `pip_spec` — a version pin or a GitHub tarball URL such as `https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/<PR>/head.tar.gz` — then reinstalls and restarts it via the component's own options flow. |
| `restart` | Restarts this server: config-entry reload in embedded mode, Supervisor self-restart in add-on mode. Standalone processes must be restarted externally. |

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
| `reset` | Removes one setting's override-file entry, returning it to its env/default value. |

Changes persist immediately but — like the web UI — most settings only take
effect after a restart (`ha_dev_manage_server` `restart`).

Backup settings and per-tool enable/disable state are separate surfaces
(Backups tab / Tools tab) and are not covered by this tool.
