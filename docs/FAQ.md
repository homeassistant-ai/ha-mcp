# FAQ & Troubleshooting

Common questions and solutions for ha-mcp setup.

## General Questions

### Do I need a Claude Pro subscription?

**No.** Claude Desktop works with a free Claude account. The MCP integration is available to all users, though free accounts have usage limits.

You can also use ha-mcp with other AI clients. See the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) for 15+ supported clients.

### Do I need the Home Assistant Add-on?

**No.** The HA add-on is just one installation method. Most users run ha-mcp directly on their computer using `uvx` (recommended for Claude Desktop). The add-on is only needed if you want to run ha-mcp inside your Home Assistant OS environment.

### What's the difference between ha-mcp and Home Assistant's built-in MCP?

| Feature | Built-in HA MCP | ha-mcp |
|---------|-----------------|--------|
| Tools | ~15 basic tools | 92+ comprehensive tools |
| Focus | Device control | Full system administration |
| Automations | Limited | Create, edit, debug, trace |
| Dashboards | No | Full dashboard management |
| Cameras | No | Screenshot and analysis |

Built-in = operate devices. ha-mcp = administer your system.

---

## Try Without Your Own Home Assistant

Want to test before connecting to your own Home Assistant? Use our public demo:

| Setting | Value |
|---------|-------|
| **URL** | `https://ha-mcp-demo-server.qc-h.net` |
| **Token** | `demo` |
| **Web UI** | Login with `mcp` / `mcp` |

Just set `HOMEASSISTANT_TOKEN` to `demo` and ha-mcp will automatically use the demo credentials.

The demo environment resets weekly. Your changes won't persist.

---

## Troubleshooting

### OAuth stopped working after upgrading to v7.0.0

v7.0.0 removed the Home Assistant URL field from the OAuth consent form to fix security vulnerabilities (SSRF and XSS). Set `HOMEASSISTANT_URL` as a server-side environment variable before starting ha-mcp. See the [OAuth migration guide](OAUTH.md#migrating-from-v6x) for instructions.

### Claude.ai says "Couldn't reach the MCP server"

**This is normal.** Claude.ai shows this error during its initial connection handshake, but the server connects successfully afterward. To verify you're actually connected:

1. Look for a **"Configure"** button on the connector — click it
2. If you see tools listed, you're connected and ready to go

You can also start a new conversation and ask Claude if it can see your Home Assistant via the MCP connection — this is the easiest way to confirm it's truly connected. Checking your server logs for successful requests (HTTP 200) after the initial error also confirms the connection is working.

This is a known Claude.ai behavior that affects all MCP servers, not just ha-mcp.

**If it genuinely won't connect** (not just the transient handshake error above): Claude.ai connects from Anthropic's servers, so the MCP URL must be reachable from the public internet — not just your LAN. A URL that works in Claude Code or a local browser can still be unreachable for Claude.ai web. Open the URL on your **phone with Wi-Fi off** (cellular): if it doesn't load there, it isn't publicly reachable (DNS / port-forward / TLS / reverse-proxy) and Claude.ai can't reach it either. Also make sure you clicked **Connect** on the connector (and, with OAuth enabled, **Allow** on the consent page) — adding the connector alone does not complete the connection.

**Check for a port in the URL.** Your connector URL is built on your Home Assistant's own public address, which must **not** contain a port such as `:8123` (or any other port). To check, open just that base address (e.g. `https://ha.example.com`, without the `/api/webhook/...` secret path) in a browser — it should bring up your HA login page. Remote clients cannot reach a URL that carries a port, even though it loads fine in your own browser. Home Assistant can still listen on 8123 internally, as long as a reverse proxy, tunnel, or 443 port-forward serves that hostname — just don't put the port in the URL you paste.

### "Terminating session: None" in server logs

**This is normal.** ha-mcp runs in stateless HTTP mode, which means each request creates and discards a temporary session. The `Terminating session: None` log message is the MCP SDK reporting this routine cleanup — the connection stays active.

### Cloudflare: LLM can't connect ("Block AI training bots")

If you're using Cloudflare and your LLM client can't connect to the MCP server (but visiting the URL in your browser works), Cloudflare's **"Block AI training bots"** setting is almost certainly the cause. This is the most common connection issue for Cloudflare users.

To disable it:

1. Log in to [Cloudflare](https://dash.cloudflare.com)
2. In the left sidebar, click **Domains**, then click **Overview**
3. Click on the domain you use for connecting to Home Assistant
4. On the right side of the page, find **"Control AI Crawlers"**
5. Under **"Block AI training bots"**, open the dropdown
6. Select **"do not block (allow crawlers)"**

![Cloudflare AI Crawlers Setting](https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg)

See [#783](https://github.com/homeassistant-ai/ha-mcp/issues/783) for more details.

**Also check geo / country blocking.** This applies to Cloudflare (WAF custom rules) and to any other reverse proxy (NGINX, Traefik, Zoraxy, etc.). Most AI/LLM services connect from US-based cloud infrastructure, so if you block US IP addresses (or only allow your own country), your client cannot connect even with AI-bot blocking disabled. Allow your AI provider's IP ranges — Claude.ai connects from Anthropic's network, `160.79.104.0/21` (see [Anthropic's IP ranges](https://platform.claude.com/docs/en/api/ip-addresses)). Your proxy's access logs will show the blocked attempts.

### macOS: "All connection attempts failed" to local Home Assistant

If ha-mcp connects to the demo server but fails to reach your local Home Assistant (`192.168.x.x`, `10.x.x.x`, etc.) on macOS, the most common causes are listed below. See [#867](https://github.com/homeassistant-ai/ha-mcp/issues/867) (Local Network Privacy), [#630](https://github.com/homeassistant-ai/ha-mcp/issues/630) (env vars not reaching ha-mcp), and [#773](https://github.com/homeassistant-ai/ha-mcp/issues/773) (Python version/read-only filesystem) for related reports.

**1. macOS Local Network Privacy (Sequoia 15+)**

macOS Sequoia silently blocks subprocess connections to local network IPs. Claude Desktop spawns `uvx` as a child process, and macOS may block its outbound LAN connections without showing a permission dialog.

- Check **System Settings → Privacy & Security → Local Network** for Claude Desktop
- If Claude Desktop is not listed, try restarting it to trigger the permission prompt

**Workaround — SSH tunnel to localhost:**

Since macOS does not restrict connections to `localhost`, an SSH port forward bypasses the restriction:

```bash
ssh -N -L 8123:localhost:8123 user@your-ha-server-ip
```

Then set `HOMEASSISTANT_URL` to `http://localhost:8123` in your config.

**2. Firewall software (Little Snitch, Lulu, etc.)**

Third-party firewalls may block `python` or `node` processes spawned by Claude Desktop from making network connections. Check your firewall rules and allow connections for these processes. See [#780](https://github.com/homeassistant-ai/ha-mcp/issues/780) for an example resolution.

**3. http:// vs https://**

Home Assistant running in container mode (Docker, K3s) uses HTTP by default. Using `https://` causes a TLS handshake error. Use `http://` unless you have explicitly configured SSL/TLS or a reverse proxy.

**4. Python version too old**

ha-mcp requires Python 3.13+. If you are on Python 3.12 or older, `uvx` installs an outdated version of ha-mcp that may have known bugs (including read-only filesystem errors). Upgrade Python:

```bash
brew install python@3.13
```

Then force a refresh:

```bash
uvx --refresh ha-mcp@latest
```

If `uvx` still uses the old Python after installing 3.13, explicitly pin it by adding `--python 3.13` to your config args:

```json
"args": ["--python", "3.13", "ha-mcp@latest"]
```

### SSL certificate errors (self-signed certificates)

If your Home Assistant uses HTTPS with a self-signed certificate or custom CA, you may see SSL verification errors.

**Docker solution:**

1. Create a combined CA bundle:
   ```bash
   cat $(python3 -m certifi) /path/to/your-ca.crt > combined-ca-bundle.crt
   ```

2. Mount it and set `SSL_CERT_FILE`:
   ```json
   {
     "mcpServers": {
       "home-assistant": {
         "command": "docker",
         "args": [
           "run", "--rm",
           "-e", "HOMEASSISTANT_URL=https://your-ha:8123",
           "-e", "HOMEASSISTANT_TOKEN=your_token",
           "-e", "SSL_CERT_FILE=/certs/ca-bundle.crt",
           "-v", "./combined-ca-bundle.crt:/certs/ca-bundle.crt:ro",
           "ghcr.io/homeassistant-ai/ha-mcp:latest"
         ]
       }
     }
   }
   ```

### Windows: pywin32 installation fails

If you see `Failed to install: pywin32` or `os error 32` ("file is used by another process") when starting ha-mcp on Windows, this is caused by two upstream bugs:

1. The MCP Python SDK requires `pywin32` on Windows even though server-only users don't need it ([python-sdk#2233](https://github.com/modelcontextprotocol/python-sdk/issues/2233))
2. `uv` has a known issue installing `pywin32` on Windows ([uv#17679](https://github.com/astral-sh/uv/issues/17679))

**Workaround — use Docker:**

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "HOMEASSISTANT_URL=http://host.docker.internal:8123",
        "-e", "HOMEASSISTANT_TOKEN=your_token",
        "ghcr.io/homeassistant-ai/ha-mcp:latest"
      ]
    }
  }
}
```

See [#672](https://github.com/homeassistant-ai/ha-mcp/issues/672) for details.

### "uvx not found" error

After installing uv, **restart your terminal** (or Claude Desktop) for the PATH changes to take effect.

**Mac:**
```bash
# Reload shell or restart terminal
source ~/.zshrc
# Or verify with full path
~/.local/bin/uvx --version
```

**Windows:**
```powershell
# Restart PowerShell/cmd after installing uv
# Or use full path
%USERPROFILE%\.local\bin\uvx.exe --version
```

### MCP server not showing in Claude Desktop

1. **Restart Claude completely** - Use Cmd+Q (Mac) or Alt+F4 (Windows), not just close the window
2. **Check config file location:**
   - Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows (traditional installer): `%APPDATA%\Claude\claude_desktop_config.json`
   - Windows (Microsoft Store): path varies by package — see the [Windows setup guide](https://homeassistant-ai.github.io/ha-mcp/guide-windows) for a detection snippet
3. **Verify JSON syntax** - No trailing commas, proper quotes
4. **Check the MCP icon** - Bottom left of Claude Desktop shows connected servers

### "Token invalid" or authentication errors

1. **Generate a new token:**
   - Home Assistant → Click your username (bottom left)
   - Security tab → Long-lived access tokens
   - Create Token → Copy immediately (shown only once)
2. **Check token format** - Don't wrap the token in quotes in your config
3. **Token expiration** - Tokens don't expire by default, but can be revoked

### Claude says it can't see Home Assistant

1. Open Claude Desktop **Settings** (gear icon)
2. Go to the **Developer** tab
3. Check **Local MCP Servers** for any errors
4. If "Home Assistant" is not listed, check your config file syntax
5. Try asking Claude: "Can you list your available tools?"

### Claude Desktop shows the server connected but exposes zero tools

**Fingerprint:** The server shows as connected and both `initialize` and `tools/list` complete successfully in `mcp.log` and the per-server log, yet the model sees no tools – and nothing surfaces an error in the UI or the logs.

**Fix:** Check the server's key in `claude_desktop_config.json` for parentheses and remove them. For example, renaming the key from `"Home Assistant (ha-mcp)"` to `"HASS ha-mcp"` (same URL, everything else unchanged) makes the full tool catalog reappear after a restart. Spaces in the key are fine; parentheses are the characters observed to trigger the drop. If in doubt, keep the key to letters, digits, spaces, `_`, and `-`.

**Why:** The Anthropic API requires every tool name to match `^[a-zA-Z0-9_-]{1,64}$` ([tool-definition docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)), and Claude Desktop appears to derive each exposed tool's namespaced name from the `mcpServers` key. Spaces in a key are fine (the reporter confirmed spaces work), but a key with `(` or `)` leaves the derived names outside that grammar, so the client discards the affected tools before it ever calls the API – which is why the drop leaves no trace in the logs. (Claude Desktop's exact key-to-name handling is not publicly documented; this explanation is inferred from the reporter's bidirectional repro – same URL, only the key changes – together with the published name constraint.)

None of the shipped example configs use parentheses in the key, so a default setup never hits this – it is specifically a hand-authored key like `Home Assistant (ha-mcp)` that trips it. This is a Claude Desktop client behavior, not a ha-mcp problem: ha-mcp's own tool names are all valid `snake_case`. See [#1743](https://github.com/homeassistant-ai/ha-mcp/issues/1743).

### Can't connect remotely? Try the Webhook Proxy add-on {#webhook-proxy}

If you're having trouble setting up remote access — TLS errors, Cloudflare configuration issues, or port forwarding problems — the **Webhook Proxy add-on** may be a simpler alternative.

Instead of requiring a dedicated tunnel to port 9583, the Webhook Proxy routes MCP traffic through Home Assistant's main port (8123) via a webhook. If you already have **Nabu Casa** or any reverse proxy pointing at your HA instance, this can be the easiest remote setup.

1. Install the **MCP Server add-on** and the **Webhook Proxy add-on** from the add-on store
2. Start the webhook proxy and restart Home Assistant when prompted
3. Copy the webhook URL from the add-on logs
4. Use that URL in your MCP client configuration

See [#784](https://github.com/homeassistant-ai/ha-mcp/issues/784) for an example where this resolved a TLS connection issue.

### ChatGPT behind a firewall? Try the community OpenAI Tunnel integration {#openai-tunnel}

ChatGPT connectors require a URL reachable from the public internet. If your Home Assistant sits behind a firewall or CGNAT and you don't want to expose it, the community-maintained [OpenAI Tunnel for HA-MCP](https://github.com/norpol/hass-codex-tunnel-mcp) integration by [@norpol](https://github.com/norpol) is an outbound-only alternative:

- It downloads, verifies, and supervises OpenAI's [`tunnel-client`](https://github.com/openai/tunnel-client) as a Home Assistant subprocess (installed as a HACS custom repository; Linux `amd64`/`aarch64` on HA OS / Supervised initially).
- The client connects your MCP server URL to an OpenAI-hosted tunnel, so ChatGPT, Codex, and other OpenAI products can reach it — no port forwarding, reverse proxy, or public URL needed.
- Create a tunnel on the [Tunnels page](https://platform.openai.com/settings/organization/tunnels) and a runtime API key with **Tunnels Read** and **Tunnels Use** permissions on the [API keys page](https://platform.openai.com/settings/organization/api-keys), point the integration at your local ha-mcp URL, and add the ChatGPT connector using the same tunnel ID.

See the [integration's README](https://github.com/norpol/hass-codex-tunnel-mcp#readme) for full setup and [#1811](https://github.com/homeassistant-ai/ha-mcp/issues/1811) for background. This is a third-party project — report tunnel issues on its tracker, not here.

### Server works but responses are slow

1. **First request is slow** - `uvx` downloads packages on first run
2. **Subsequent requests** - Should be faster (packages cached)
3. **Alternative** - Use Docker for consistent performance

### Tools are missing or using old version

If you're seeing fewer tools than expected or outdated behavior, `uvx` may be using a cached old version.

**Solution:**

```bash
# Clear the uv cache
uv cache clean

# Force refresh to latest version
uvx --refresh ha-mcp@latest
```

**Verify the version:**
```bash
uvx ha-mcp@latest --version
```

The version should match the [latest release](https://github.com/homeassistant-ai/ha-mcp/releases/latest). If you see a much older version, the cache needs clearing.

---

---

## Configuration Options

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Your Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token (or `demo` for demo env) | - | Yes |
| `BACKUP_HINT` | Backup recommendation level | `normal` | No |

### Backup Hint Modes

| Mode | Behavior |
|------|----------|
| `strong` | Suggests backup before first modification each day/session |
| `normal` | Suggests backup only before irreversible operations (recommended) |
| `weak` | Rarely suggests backups |
| `auto` | Same as normal (future: auto-detection) |

### Entity visibility filter (opt-in)

By default the agent sees every entity. If auto-generated diagnostic or helper
entities clutter search and overview results, you can hide a chosen set of them
from the *collection* read tools (`ha_search`, `ha_get_overview`). This is
**noise reduction, not access control** – a hidden entity is still returned by a
direct `ha_get_state` / `ha_get_entity` on its `entity_id`, and still appears in
automation, dashboard, and template content, so do not rely on it as a security
boundary.

**Reads only – it does not gate control tools.** The filter scopes what the
*collection read* tools return. It does **not** stop an agent from calling a
service on a hidden `entity_id`: gating writes is a separate concern handled by
the Tool Security Policies engine (which matches on a call's arguments), not by
visibility. Visibility is deliberately read-scoping only, precisely because it
is noise reduction and cannot be a security boundary (content-bearing reads such
as automation and template bodies would leak hidden entities anyway).

The easiest way to configure it is the **Entity Visibility** tab in the ha-mcp
settings UI (enable toggle, category checkboxes, area/label fields, per-entity
denylist). It reads and writes the same file described below, so either surface
works.

The filter is off until `entity_visibility.json` exists in the ha-mcp data
directory (the same directory as `tool_policy.json`; `/data` in the add-on) with
`"enabled": true`:

```json
{
  "version": 1,
  "enabled": true,
  "exclude_categories": ["diagnostic", "config"],
  "exclude_hidden": false,
  "deny_entity_ids": [],
  "exclude_areas": [],
  "exclude_labels": [],
  "allow_entity_ids": [],
  "allow_areas": [],
  "allow_labels": [],
  "respect_assist_exposure": false
}
```

The filter is a conjunction of independent dimensions: an entity is shown only if
it passes every active one.

- **Excludes / denylist.** An entity is hidden when its `entity_category` is in
  `exclude_categories`, its `entity_id` is in `deny_entity_ids`, or its area/label
  is in `exclude_areas` / `exclude_labels`. `exclude_categories` accepts only Home
  Assistant's two entity categories (`diagnostic`, `config`); an unknown value is
  ignored and surfaced as a `warnings` entry on the next read rather than silently
  doing nothing. Set `exclude_hidden: true` to also fold in entities already
  marked hidden in Home Assistant.
- **Allowlist.** The moment any of `allow_entity_ids` / `allow_areas` /
  `allow_labels` is non-empty, the filter inverts to *restrict* mode: only
  entities matching an allowlist stay visible and everything else – including
  entities added later – is hidden. Leave all three empty to keep the allowlist
  off. `deny_entity_ids` still wins over an allow match — and so does any
  `exclude_*` match: an entity an allowlist would admit but an
  `exclude_categories` / `exclude_areas` / `exclude_labels` also hides stays
  hidden (every dimension can only hide, so any one hide is enough — the allow
  dimensions cannot un-hide what another dimension excluded).
- **Respect Assist exposure.** With `respect_assist_exposure: true` the filter
  hides entities not effectively exposed to Home Assistant's Assist
  (`conversation`) assistant, mirroring `async_should_expose` (an explicit
  per-entity exposure override wins; otherwise, if the instance exposes new
  entities, the entity's domain and device-class defaults decide). Because HA
  offers no single "effective exposure" API, the decision is reconstructed
  client-side from two extra websocket reads per search — the set of entities
  explicitly exposed to the assistant (`expose_entity/list`, which reports only
  the *exposed* ones) and the "expose new entities" flag that drives the default
  branch; if either read fails the dimension is skipped with a `warnings` note
  rather than hiding everything. A registry entity's explicit override — exposed
  *or* un-exposed — is read directly from the entity-registry `options` the
  registry list already carries, so an explicit un-expose is honored. One residual
  limit: for an entity that lives only in the state machine (a YAML/template entity
  with no entity-registry entry), HA surfaces it through `expose_entity/list` only
  when it is *exposed*; an explicit un-expose cannot be observed there, so such an
  entity falls to its domain/device-class default and stays visible (fail-open).

`version` drives optimistic-concurrency for the settings UI (it bumps
on each save so two tabs can't clobber each other); when hand-editing the file,
leave it as-is. The config is read live per request, so edits apply on the next
call; a missing or invalid file leaves the filter off (and, when enabled but the
registry read degrades, results are unfiltered with a `warnings` note rather than
silently wrong).

---

## Feedback & Help

We'd love to hear how you're using ha-mcp!

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Share how you use it, ask questions, show off your automations
- **[GitHub Issues](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs or request features
- **[Home Assistant Forum](https://community.home-assistant.io/t/brand-new-claude-ai-chatgpt-integration-ha-mcp/937847)** — Community discussion thread
