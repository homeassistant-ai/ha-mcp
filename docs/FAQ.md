# FAQ & Troubleshooting

Common questions and solutions for ha-mcp setup.

## General Questions

### Do I need a Claude Pro subscription?

**No.** Claude Desktop works with a free Claude account. The MCP integration is available to all users, though free accounts have usage limits.

You can also use ha-mcp with other AI clients. See the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) for 15+ supported clients.

### Do I need the Home Assistant app (add-on)?

**No.** The HA app is just one installation method. Most users run ha-mcp directly on their computer using `uvx` (recommended for Claude Desktop). To run ha-mcp inside Home Assistant itself, you can install it as the app (Home Assistant OS / Supervised) or via the [HA-MCP custom component](#custom-component-ha_mcp_tools)'s in-process server, which works on every installation type.

If you *do* run the app, you don't also need `uvx` — that would be a second, separate instance. HTTP-native clients (Cursor, Windsurf, Claude Code) connect straight to the app's URL, no proxy needed. Codex is HTTP-native too, but as of early 2026 its HTTP MCP support has a [known initialization bug](https://github.com/openai/codex/issues/11284) where it loads no tools; if you hit that, run ha-mcp locally over stdio with `uvx` instead.

**Don't run both for the same client.** If you switch to the app, remove any local/stdio entry from your client config — a Claude Desktop server running `uvx ha-mcp@latest` with `HOMEASSISTANT_URL` / `HOMEASSISTANT_TOKEN` is the local version and bypasses the app entirely. Pointing both at the same Home Assistant can leave the connection wedged until you restart. The app's Claude Desktop config uses `uvx fastmcp-remote` with the app's URL and **no** token.

### What's the difference between ha-mcp and Home Assistant's built-in MCP?

| Feature | Built-in HA MCP | ha-mcp |
|---------|-----------------|--------|
| Tools | ~15 basic tools | 87 comprehensive tools |
| Focus | Device control | Full system administration |
| Automations | Limited | Create, edit, debug, trace |
| Dashboards | No | Full dashboard management |
| Cameras | No | Screenshot and analysis |

Built-in = operate devices. ha-mcp = administer your system.

### How do I open the ha-mcp settings page?

ha-mcp ships a web settings page where you can enable, disable, and pin individual MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests. How you reach it depends on how ha-mcp is running:

- **Claude Desktop / Claude Code / any stdio install (`uvx ha-mcp`):** a localhost settings server spawns automatically alongside the MCP process. The easiest way to find the URL is to **ask the AI** something like *"how do I open the ha-mcp settings page?"* — the URL is included in `ha_get_overview`'s response. You can also read it directly from `~/.ha-mcp/ui.url` (Windows: `%USERPROFILE%\.ha-mcp\ui.url`). The URL is bound to `127.0.0.1` only and gated by a random secret path generated on first launch and reused afterwards, so the URL stays stable. To rotate it, stop the sidecar with the **Permanently disable settings server** button at the bottom of the settings page's Server tab, then delete **both** `~/.ha-mcp/ui.state` and `~/.ha-mcp/ui.url`, and remove the `~/.ha-mcp/settings_ui_disabled` sentinel that button writes so the next launch spawns again. Deleting `ui.state` alone is not enough: the next startup re-seeds it from a surviving `ui.url` and serves the same secret path, and since the sidecar outlives the client that spawned it, quitting your client does not stop it.
- **HA Custom Component (in-process server):** open the admin-only **HA-MCP** panel in the Home Assistant sidebar — it serves the settings page through Home Assistant itself, no URL or secret needed.
- **HA App:** easiest is the **"Open Web UI"** button on the app page (served over Home Assistant ingress — no secret needed). For direct access outside the HA UI **on your trusted LAN**, the page lives behind the app's secret path: `http://<home-assistant-ip>:9583/<secret-path>/settings`. That URL is plain HTTP, so it carries the secret path in cleartext — don't use it across an untrusted network. Reach it remotely through ingress or an HTTPS reverse proxy instead (see the Remote bullet below). Find `<secret-path>` in the app's **Configuration** tab ("Secret path override") or in `/data/secret_path.txt`. The bare `:9583/settings` without the secret path is rejected for non-ingress callers.
- **`ha-mcp-web` / Docker (HTTP):** append `/settings` to your MCP secret-path URL. The default `MCP_SECRET_PATH` is `/mcp`, so the page is at `http://<host>:8086/mcp/settings`; if you set a custom secret path, use that instead (e.g. `http://<host>:8086/private_xxx/settings`).
- **Remote (Cloudflare Tunnel / reverse proxy):** same as Docker/HTTP — the page sits under your secret path wherever the MCP endpoint is reachable, e.g. `https://<your-host>/<secret-path>/settings`.

Tool enable/disable and the server-wide feature flags apply on the next MCP-server restart, after which your client needs its tool list refreshed. Entity Visibility edits apply on the next call, and tool-approval decisions apply immediately. To stop the stdio sidecar entirely, click the "Permanently disable settings server" button on the page, set `HA_MCP_DISABLE_SETTINGS_UI=1` in your MCP client config, or create an empty `~/.ha-mcp/settings_ui_disabled` file.

### My settings reset every time I re-create the Docker container

ha-mcp stores its tool configuration, feature flags, backup settings and OAuth client registrations under `~/.ha-mcp` — which is `/home/mcpuser/.ha-mcp` inside the container. That lives in the container's writable layer, so it disappears whenever the container is replaced (`docker rm`, `docker compose down`, pulling a new image, or any `--rm` run). Mount a volume there to keep it:

```bash
docker run -d --name ha-mcp -p 8086:8086 \
  -v ha-mcp-data:/home/mcpuser/.ha-mcp \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```

Or in compose:

```yaml
services:
  ha-mcp:
    volumes:
      - ha-mcp-data:/home/mcpuser/.ha-mcp

volumes:
  ha-mcp-data:
    name: ha-mcp-data
```

Keep the `name:` line — without it Compose creates `<project>_ha-mcp-data` instead, which is not the volume the `docker run` command above or the `docker volume rm` below refer to.

A volume mount stays writable even with `read_only: true`, so keep it when you harden the container — without it ha-mcp can't write to `~/.ha-mcp` under a read-only root filesystem and falls back to a temporary directory that's wiped on every restart. To store the data elsewhere, mount your own path and set `HA_MCP_CONFIG_DIR` to it. This doesn't apply to the HA app (add-on) or the custom component, which persist to Home Assistant's own storage automatically.

Whatever you mount must be writable by the UID the container actually runs as. A named volume is initialised as UID 999 (the image's `mcpuser`) and needs nothing further; a host bind mount needs `chown 999:999` first; and if you run with `--user` to override the UID, chown the mounted directory to that UID instead — a named volume stays owned by 999 and the overridden user can't write to it. If ha-mcp still can't persist, it says so at startup — look for *"Cannot write ha-mcp data to ... data will NOT persist across restarts"* in `docker logs ha-mcp`. A volume created by an older image can be left owned by root. Repair the ownership in place rather than deleting the volume, which would destroy your settings, backups, and OAuth client registrations:

```bash
docker run --rm -v ha-mcp-data:/data alpine chown -R 999:999 /data
```

Only if that fails and you accept losing the stored data, `docker volume rm ha-mcp-data` lets the current image recreate it from scratch — back the volume up first.

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

### `ha-mcp-sse` is gone / my SSE config stopped connecting

The legacy `ha-mcp-sse` entry point (deprecated HTTP+SSE transport on port 8087)
was removed — the MCP specification deprecated the HTTP+SSE transport. Switch to
`ha-mcp-web`, which serves Streamable HTTP at `/mcp` on port 8086, and change any
SSE-style client config accordingly (Gemini CLI users: use the `httpUrl` key, not
`url`). An SSE / `--transport sse` client config pointed at `ha-mcp-web` returns
`405` and won't connect.

### OAuth stopped working after upgrading to v7.0.0

v7.0.0 removed the Home Assistant URL field from the OAuth consent form to fix security vulnerabilities (SSRF and XSS). Set `HOMEASSISTANT_URL` as a server-side environment variable before starting ha-mcp.

```bash
# Docker
docker run -d -p 8086:8086 \
  -v ha-mcp-data:/home/mcpuser/.ha-mcp \
  -e HOMEASSISTANT_URL=https://your-ha-instance.example.com \
  -e MCP_BASE_URL=https://your-mcp-server.example.com \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-oauth

# uvx
HOMEASSISTANT_URL=https://your-ha-instance.example.com \
MCP_BASE_URL=https://your-mcp-server.example.com \
uvx --from=ha-mcp@latest ha-mcp-oauth
```

The consent form now accepts only the token. See the [OAuth migration guide](OAUTH.md#migrating-from-v6x) for instructions.

### Claude.ai says "Couldn't reach the MCP server"

**This is normal.** Claude.ai shows this error during its initial connection handshake, but the server connects successfully afterward. To verify you're actually connected:

1. Look for a **"Configure"** button on the connector — click it
2. If you see tools listed, you're connected and ready to go

You can also start a new conversation and ask Claude if it can see your Home Assistant via the MCP connection — this is the easiest way to confirm it's truly connected. Checking your server logs for successful requests (HTTP 200) after the initial error also confirms the connection is working.

This is a known Claude.ai behavior that affects all MCP servers, not just ha-mcp.

**If it genuinely won't connect** (not just the transient handshake error above): Claude.ai connects from Anthropic's servers, so the MCP URL must be reachable from the public internet — not just your LAN. A URL that works in Claude Code or a local browser can still be unreachable for Claude.ai web. Open the URL on your **phone with Wi-Fi off** (cellular): if it doesn't load there, it isn't publicly reachable (DNS / port-forward / TLS / reverse-proxy) and Claude.ai can't reach it either. Also make sure you clicked **Connect** on the connector (and, with OAuth enabled, **Allow** on the consent page) — adding the connector alone does not complete the connection.

### Claude.ai connects from my browser but the connector fails — ports, firewalls, timeouts

Hosted Claude surfaces (claude.ai, Claude Desktop connectors) reach your server from *Anthropic's backend*, not from your browser. Three environment rules apply that no server-side setting can work around:

- **Port `443` only.** The connector URL must use standard HTTPS with no explicit port. Anthropic's backend only connects on 443, so `https://ha.example.com:8123/...` is unreachable from a hosted connector even though it loads fine in your own browser and passes an external HTTP check. Put a reverse proxy, tunnel, or 443 port-forward in front and keep Home Assistant on 8123 internally, then paste the port-less URL. To check, open just the base address (e.g. `https://ha.example.com`, without the `/api/webhook/...` secret path) in a browser — it should bring up your HA login page.
- **Allowlist Anthropic's egress range.** Backend traffic originates from `160.79.104.0/21`. A WAF, geo-block, or bot filter covering that range (Cloudflare's AI-crawler rules included) breaks registration, token exchange, and the post-OAuth handshake even though your browser works fine. See [Anthropic's IP ranges](https://platform.claude.com/docs/en/api/ip-addresses) and the Cloudflare entry below.
- **Answer fast.** Claude waits at most 10 seconds for discovery, registration, and token responses (30 for refresh), per [Anthropic's connector authentication documentation](https://claude.com/docs/connectors/building/authentication). Slow reverse proxies or cold paths cause intermittent connection failures.

**Tailscale Funnel: use port `443`.** Funnel can also serve on the alternate HTTPS ports it offers (`8443`, `10000`), but Claude.ai's connector backend does not reliably reach non-standard ports: the connection fails identically in every auth mode, and no request from Anthropic's range (`160.79.104.0/21`) ever reaches the server — nothing appears in any log. Use standard port `443` instead, where the same setup connects on the first try. The official Tailscale app's built-in **"Share Home Assistant with Serve or Funnel"** option already exposes Home Assistant on `443`, so use that hostname in the connector URL (same webhook path). See [#2080](https://github.com/homeassistant-ai/ha-mcp/issues/2080).

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

2. Mount it and set `SSL_CERT_FILE`. The bind source must be an **absolute** host path — your MCP client spawns `docker` from its own working directory, not the one you built the bundle in, so a `./` relative source resolves somewhere else:
   ```json
   {
     "mcpServers": {
       "home-assistant": {
         "command": "docker",
         "args": [
           "run", "--rm", "-i",
           "-e", "HOMEASSISTANT_URL=https://your-ha:8123",
           "-e", "HOMEASSISTANT_TOKEN=your_token",
           "-e", "SSL_CERT_FILE=/certs/ca-bundle.crt",
           "-v", "/absolute/path/to/combined-ca-bundle.crt:/certs/ca-bundle.crt:ro",
           "ghcr.io/homeassistant-ai/ha-mcp:latest"
         ]
       }
     }
   }
   ```

### mcp-proxy fails with `ImportError: cannot import name 'request_ctx'`

A previously working `uvx mcp-proxy` config stops connecting and the client shows "Server disconnected". Running the command by hand shows:

```text
from mcp.server.lowlevel.server import request_ctx
ImportError: cannot import name 'request_ctx' from 'mcp.server.lowlevel.server'
```

**Cause:** the `mcp` SDK released version 2.0.0, which removed `request_ctx`. mcp-proxy depends on `mcp` without an upper version bound, so `uvx` installs the incompatible 2.x release. Clearing the uv cache does not help — a fresh install picks the same version.

This only affected clients that reached an HTTP ha-mcp deployment through the mcp-proxy bridge (Claude Desktop and JetBrains, when this FAQ entry was written). The local stdio setup — `uvx ha-mcp@latest` — is unaffected, and so are the app (add-on), Docker, and the custom component themselves: ha-mcp already pins the SDK below 2.0.

**If your client is JetBrains:** drop the bridge entirely rather than switching bridges. JetBrains IDEs support Streamable HTTP natively via the MCP Servers panel. The [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) now generates a plain `{"mcpServers": {"home-assistant": {"url": "..."}}}` entry with no `command`/`args` at all. The fastmcp-remote fix below still applies to Claude Desktop and other stdio-only clients.

**Fix (Claude Desktop and other stdio-only clients):** switch the bridge to `fastmcp-remote`, the stdio bridge published by the FastMCP project. It pins its dependency on the MCP SDK to a bounded range, so an SDK release cannot break it the way it broke mcp-proxy. Replace the whole server entry with:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": [
        "fastmcp-remote",
        "http://192.168.1.100:9583/private_your_secret_path"
      ]
    }
  }
}
```

Keep your own connect URL. No `--transport` flag is needed: fastmcp-remote defaults to Streamable HTTP, which is what ha-mcp serves. Restart the client after saving (Claude Desktop: **File → Exit**, then reopen; closing the window is not enough). The [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) generates this config for stdio-only clients.

**Prefer to stay on mcp-proxy?** Adding `"--with", "mcp<2.0.0"` to its `args` also works. `--with` is a global `uv` option, so it must come *before* the package name. That pin has to be revisited on the next SDK major; switching bridges does not. See [#2073](https://github.com/homeassistant-ai/ha-mcp/issues/2073).

### uvx fails with `invalid peer certificate: UnknownIssuer`

If `uvx` can't download ha-mcp (or `fastmcp-remote`, if you connect to the app through it) and the log shows a certificate error fetching from PyPI, the failure is in `uv`'s package download — it never reaches Home Assistant:

```text
Failed to fetch: https://pypi.org/simple/ha-mcp/
  invalid peer certificate: UnknownIssuer
```

In Claude Desktop this often shows up as the server briefly connecting, then "transport closed unexpectedly" — `uvx` exits when it can't fetch the package.

**Cause:** `uv` (which backs `uvx`) ships its own bundled root-certificate store and ignores the operating-system certificate store by default. If anything on your machine intercepts HTTPS — a corporate proxy, Zscaler, or antivirus with HTTPS/SSL scanning (Kaspersky, ESET, Bitdefender, etc.) — it presents a certificate chained to a root your OS trusts but `uv`'s bundled store does not, producing `UnknownIssuer`. This is most common on Windows.

**Fix:** point `uv` at the OS native certificate store by adding `UV_NATIVE_TLS=1` to the server's `env` block:

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token",
        "UV_NATIVE_TLS": "1"
      }
    }
  }
}
```

Connecting to the HA app through `uvx fastmcp-remote` instead? Add the same `UV_NATIVE_TLS=1` entry to that config's `env` block. The equivalent `--native-tls` flag also works, but it is a global `uv` option, so it must come before the package name. See [#1506](https://github.com/homeassistant-ai/ha-mcp/issues/1506).

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
        "-v", "ha-mcp-data:/home/mcpuser/.ha-mcp",
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

**Claude Desktop note:** Claude Desktop does **not** inherit your shell's PATH. If `uvx` is not found even after restarting, use the absolute path in your config instead of `uvx`. Find it with `which uvx` (macOS/Linux) or `where uvx` / `Get-Command uvx` (Windows PowerShell), then set `"command": "/Users/<you>/.local/bin/uvx"` or the equivalent Windows path.

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

### Can't connect remotely? Try the Webhook Proxy app

Using the [HA-MCP custom component](#custom-component-ha_mcp_tools)'s in-process server? Remote access is built in — its webhook connect URL already works through Nabu Casa or any reverse proxy pointed at Home Assistant, so you don't need the Webhook Proxy app. The rest of this answer applies to the app (add-on), Docker, and pip installs.

If you're having trouble setting up remote access — TLS errors, Cloudflare configuration issues, or port forwarding problems — the **Webhook Proxy app** may be a simpler alternative.

Instead of requiring a dedicated tunnel to port 9583, the Webhook Proxy routes MCP traffic through Home Assistant's main port (8123) via a webhook. If you already have **Nabu Casa** or any reverse proxy pointing at your HA instance, this can be the easiest remote setup.

1. Install the **MCP Server app** and the **Webhook Proxy app** from **Settings > Apps > Install app**
2. Start the webhook proxy and restart Home Assistant when prompted
3. Copy the webhook URL from the app logs
4. Use that URL in your MCP client configuration

See [#784](https://github.com/homeassistant-ai/ha-mcp/issues/784) for an example where this resolved a TLS connection issue.

### Webhook Proxy: securing the URL

By default the Webhook Proxy app registers an **unauthenticated** webhook endpoint. The webhook URL itself is the shared secret — anyone with the full URL can reach your MCP server, which exposes powerful Home Assistant control. Treat the URL like a password.

**Don't share the URL**

- Avoid pasting it into screenshots, log paste-bins, public configs, or chat transcripts.
- Mask the part after `/api/webhook/` if you have to share anything.
- Anyone with the full URL can call your MCP tools.

**Rotating the URL if it leaks**

1. Stop the Webhook Proxy app.
2. Delete `/data/webhook_id.txt` from the app's filesystem (e.g. via SSH/Terminal app).
3. Start the app. A new webhook ID and URL are generated on first launch.
4. Copy the new URL from the app logs into your MCP client(s). The old URL stops working immediately.

**Reinstalling the app also changes the URL.** Uninstalling wipes the app's `/data` (where `webhook_id.txt` lives), so the next start generates a fresh webhook ID and overwrites `/config/.mcp_proxy_config.json` with it. Update your MCP client — and re-add the Claude.ai connector — with the new URL afterwards.

**Optional: Enable OAuth (Beta)**

For a real auth layer on top of the URL secret, the Webhook Proxy app (v1.1.0 and later) ships an optional OAuth 2.1 mode. Toggle **Show unused optional configuration options**, turn **Enable OAuth (Beta)** on, set **OAuth Mode** to `legacy` if you want the Client ID + Secret flow described here (a first-time enable with the mode left unset defaults to `ha_auth`, which generates no credentials), leave Client ID and Client Secret blank, and restart the app. Legacy mode also needs a **full Home Assistant restart** (a Repair prompts you) — restarting only the app is not enough; `ha_auth` needs no Home Assistant restart. In legacy mode the app generates a strong Client ID and Client Secret on first start, persists them at `/data/oauth_creds.json`, and prints them in the app log.

That is the app's `legacy` OAuth mode. Webhook Proxy 3.x defaults a first-time enable to `ha_auth` instead: you sign in with your Home Assistant account and no Client ID or Secret exists. In Claude.ai's connector wizard, `ha_auth` is **Always required** + **Use Anthropic's hosted client metadata** (both auto-detected); for `legacy`, choose **Use your own OAuth client** and paste the Client ID and Client Secret from the app log. Click **Add**, then the connector's **Connect** button — Claude.ai handles the rest of the OAuth handshake. When the toggle is off, the webhook URL behaves exactly as before with no auth check.

**OAuth flow end-to-end**

1. Claude.ai redirects your browser to `https://<host>/authorize?...` with PKCE parameters.
2. The app serves a consent page showing the redirect destination — verify it's Claude.ai's callback URL.
3. Click **Allow**. The app issues a one-time auth code and redirects back to Claude.ai.
4. Claude.ai exchanges the code at `https://<host>/token` using your Client ID + Client Secret + the PKCE verifier. The app returns a 1-hour access token and a 30-day refresh token, both HMAC-signed.
5. From then on Claude.ai sends every MCP request with `Authorization: Bearer <token>`; expired access tokens are refreshed automatically.

**The Client Secret is the real security boundary**

The `/authorize` consent page is reachable without being logged into Home Assistant or Nabu Casa — that's how OAuth works (the consent page must be reachable from the OAuth client's browser session). What stops an attacker who clicks "Allow" on a phishing authorize URL: the resulting auth code is bound to PKCE and useless without your **OAuth Client Secret** at the token endpoint. So the Client Secret is the actual gate, not the consent page.

**Treat the Client Secret like a password.** Don't paste it into screenshots, support threads, or public configs. If you suspect it has leaked — or want a clean slate after sharing logs for debugging — rotate it immediately. After rotation, any tokens issued under the old credentials stop refreshing, forcing the client to redo OAuth.

**Rotating OAuth credentials**

- **From the app UI (recommended):** turn on **Regenerate OAuth Credentials on Next Start**, save, restart the app. The app wipes the stored credentials, generates fresh ones, prints them in the log, and auto-clears the regenerate toggle. Update your MCP client to match. Takes ~30 seconds.
- **Custom values:** type new strings into the Client ID and Client Secret fields and restart — your values override the stored file.
- **Filesystem:** stop the app, delete `/data/oauth_creds.json`, start the app. Equivalent to the UI option but requires SSH/Terminal access.

**Getting "Invalid client id" — or OAuth/credential changes not taking effect**

This applies to `legacy` mode (`ha_auth` needs only an app restart). Legacy mode's OAuth provider views are bound into Home Assistant's HTTP layer when the integration first loads, and HA can't re-register or drop them on a config reload (the webhook endpoint itself re-registers on reload — it's specifically the OAuth views). So switching legacy mode on/off, regenerating its credentials, or reinstalling the app in legacy mode only takes effect after a **full Home Assistant restart** (Settings → System → Restart) — reloading the integration or restarting the app is not enough. After restarting, delete and re-add the Claude.ai connector with the current URL (and current Client ID/Secret, if OAuth is on).

Beta status: the OAuth flow is built against the MCP 2025-06-18 spec and tested with Claude.ai. Other clients' OAuth coverage may vary. Report issues on GitHub.

### ChatGPT behind a firewall? Try the community OpenAI Tunnel integration

ChatGPT connectors require a URL reachable from the public internet. If your Home Assistant sits behind a firewall or CGNAT and you don't want to expose it, the community-maintained [OpenAI Tunnel for HA-MCP](https://github.com/norpol/hass-codex-tunnel-mcp) integration by [@norpol](https://github.com/norpol) is an outbound-only alternative:

- It downloads, verifies, and supervises OpenAI's [`tunnel-client`](https://github.com/openai/tunnel-client) as a Home Assistant subprocess (installed as a HACS custom repository; Linux `amd64`/`aarch64` on HA OS / Supervised initially).
- The client connects your MCP server URL to an OpenAI-hosted tunnel, so ChatGPT, Codex, and other OpenAI products can reach it — no port forwarding, reverse proxy, or public URL needed.
- Create a tunnel on the [Tunnels page](https://platform.openai.com/settings/organization/tunnels) and a runtime API key with **Tunnels Read** and **Tunnels Use** permissions on the [API keys page](https://platform.openai.com/settings/organization/api-keys), point the integration at your local ha-mcp URL, and add the ChatGPT connector using the same tunnel ID.

See the [integration's README](https://github.com/norpol/hass-codex-tunnel-mcp#readme) for full setup and [#1811](https://github.com/homeassistant-ai/ha-mcp/issues/1811) for background. This is a third-party project — report tunnel issues on its tracker, not here.

### Server works but responses are slow

1. **First request is slow** - `uvx` downloads packages on first run
2. **Subsequent requests** - Should be faster (packages cached)
3. **Alternative** - Use Docker for consistent performance

### Claude Desktop: a write tool hangs for 4 minutes, reads work fine

This is a Claude Desktop bug, not an ha-mcp bug. It affects tools served by
MCP servers Claude Desktop runs locally: anything in
`claude_desktop_config.json` (a direct stdio server or a bridge such as
`mcp-remote`, `fastmcp-remote` or `mcp-proxy`), Desktop Extensions, and
Desktop's own Filesystem connector. claude.ai custom connectors and Claude Code
are not affected.

With a tool set to **Needs approval**, Desktop shows the approval dialog while
the model is still generating the call's arguments. Clicking **Allow once**
before generation finishes silently drops the call: it never reaches the bridge
or the server, and Desktop reports "No result received … after waiting 4
minutes". The longer the arguments, the wider the window, which is why
dashboard, automation, script and helper writes hit it most. A dropped call
never reached Home Assistant, but the same 4-minute timeout can also hide a
call that did land and lost only its result, so read the target back before
repeating a write that is not idempotent. Tracked upstream as
[anthropics/claude-code#92014](https://github.com/anthropics/claude-code/issues/92014)
(a second Desktop bug,
[#80012](https://github.com/anthropics/claude-code/issues/80012), drops
in-flight calls when several conversations share one local server). Our
thread: [#2367](https://github.com/homeassistant-ai/ha-mcp/issues/2367).

Workarounds, any one of them:

1. **Wait a few seconds before clicking Allow once**, longer for large
   dashboard writes. Desktop gives no "generation finished" signal, so this
   is timing-based.
2. **Set the ha-mcp write tools to Always allow**: Settings → Connectors →
   your server → Tool permissions.
3. **Keep manual approval, but in ha-mcp instead of Desktop**: set the tools to
   Always allow in Desktop and enable **Tool Security Policies** with a
   require-approval rule on the write tools. The call is dispatched
   immediately, so the race never happens, and you approve it in the Tool
   Security Policies tab of the settings UI.
4. Use a claude.ai custom connector or Claude Code instead of a Desktop local
   server.

Related: Claude Desktop 2.110.0 rejects omitted optional parameters
("expected nonoptional, received undefined"). Update Claude Desktop to
2.2553.0 or later, which fixes it; if you cannot update, see
[#2472](https://github.com/homeassistant-ai/ha-mcp/issues/2472) for the
downgrade workaround.

#### HTTP transport diagnostics

If the hang does not match the above, for HTTP connections (including a local
stdio-to-HTTP bridge) **Settings → Advanced → Diagnostics** offers two
independent experiments. Both default to **off** and require a server restart:

- **HTTP transport diagnostics** (`HAMCP_HTTP_TRANSPORT_DIAGNOSTICS=true`):
  logs request/response byte counts, elapsed time, body completion, observed
  disconnects and exception types at INFO level. Each request gets a server-generated
  trace identifier. No bodies, credentials, secret paths or client request IDs
  are logged. The toggle automatically enables INFO for this diagnostic logger,
  including when Home Assistant defaults to WARNING, and restores the previous
  level when the HTTP app stops. Other loggers are unchanged. Explicit Home
  Assistant overrides for `ha_mcp.http_transport` and logging filters still apply.
- **JSON responses instead of streaming** (`HAMCP_HTTP_JSON_RESPONSE=true`):
  asks FastMCP to return a single JSON response instead of an SSE stream.
  This affects all HTTP clients connected to that server and removes streamed
  progress notifications from those responses. Tool results are unchanged.

Diagnostics can help distinguish an incomplete upload from an incomplete
response. `response_complete=True` means the ASGI server accepted the final
body event; it does **not** prove the client received or processed the result.
These options are experiments, not a confirmed fix for Claude Desktop hangs.
They do not affect a direct stdio server or require a custom component update.
Turn them off and restart to restore the previous HTTP behavior, including any
existing FastMCP JSON-response configuration.

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

### ChatGPT doesn't show new or newly enabled tools (stale tool list)

ChatGPT (web, including Codex Work Mode) caches a connector's tool list and sometimes keeps serving the stale list even after you enable new tools on the server, restart it, and remove and re-add the connector under the same name. Newly enabled tools (for example the beta filesystem/YAML tools) simply never appear in ChatGPT's tool list, even though the server logs show them registered — and other tools on the stale connection may fail with MCP internal errors.

**Solution:** delete the connector and create a new one with a **different name** — ChatGPT then fetches a fresh tool list. Re-adding it under the original name is not enough; the cached list survives the re-add.

### Antigravity client troubleshooting

- **"Unexpected server output" error:** Add `FASTMCP_SHOW_SERVER_BANNER=false` to your stdio env config. This disables the startup banner that Antigravity misinterprets as unexpected output.
- **"EOF" errors:** Use absolute paths for the command, not relative paths.
- **First run timeout:** Run `uvx ha-mcp@latest --version` in your terminal first to download and cache the package before Antigravity tries to start it.
- **Tools load but fail when called:** Try switching to stdio mode instead of HTTP. HTTP mode can experience "connection closed" or reconnection errors with this client.
- **Connection issues after config changes:** Restart the Agent session in Antigravity after saving any config changes.

### Claude.ai connection issues

Claude.ai connector setup is known to be flaky, but usually works after repeated attempts. Enter the URL, click **Continue**, and keep the settings marked **Detected**. If authentication is not auto-detected, the connection may not be working: **delete any existing connector and create a new one, then try again**. You can try selecting settings manually, but connections typically only work when the settings are auto-detected, so manually forcing them — including **None / No login** — may not help.

If you installed the embedded HA-MCP server, make sure you have **restarted Home Assistant** after installation so everything registers properly.

If tools stop responding or the connector appears disconnected in Claude.ai:

1. **Restart both Claude.ai and the MCP server.** Refresh the Claude.ai page in your browser and restart the ha-mcp process (or Docker container). Either side can hold stale connection state. An intermittent connect failure is usually a transient tunnel/relay hiccup, so a restart and retry often clears it.
2. A `405 Method Not Allowed` on a `GET` in your ha-mcp logs is **normal** and not the cause of a failed connect. Claude.ai pre-flights with a `GET` and the Streamable HTTP MCP endpoint only accepts `POST` (and `DELETE`), not `GET`, so a `405` shows up even on a successful connection (the log annotates it *NORMAL for most non-SSE connections*).
3. Check that your tunnel (Cloudflare, ngrok, etc.) is still running and the URL has not changed.
4. Verify the server is reachable by visiting the MCP URL directly in your browser — you should see a response from the server (with OAuth enabled on the webhook proxy, an `Unauthorized` response is expected and correct).
5. **Reachability from Anthropic's servers:** Claude.ai connects from the cloud, not your network — a URL that works in Claude Code or your browser (both on your LAN) can still be unreachable for Claude.ai web. Open the URL on your **phone with Wi-Fi off**; if it doesn't load, it isn't publicly reachable and Claude.ai can't reach it either.
6. **Don't forget to click *Connect* on the connector** — and, with OAuth enabled, click **Allow** on the consent page. Adding the connector alone does not complete the connection.
7. **URL works in your browser but the LLM can't connect?** Your reverse proxy is filtering the AI client — see [Cloudflare: LLM can't connect](#cloudflare-llm-cant-connect-block-ai-training-bots) above (Cloudflare's "Block AI training bots" and geo/country blocking are the usual causes).

### Google Gemini Spark: legacy mode is verified; ha_auth expected as of component 2.0.0

Gemini Spark's custom connected apps are OAuth-only, and they authenticate using a cross-origin redirect URI (a Client ID Metadata Document). Home Assistant core's native OAuth provider only fetches these from 2026.9 onward ([home-assistant/core#176286](https://github.com/home-assistant/core/pull/176286), fixing [#176282](https://github.com/home-assistant/core/issues/176282)); older cores reject Spark's authorization request with **"Invalid redirect URI"**.

As of component 2.0.0, the HA-MCP component validates Client ID Metadata Documents itself and hands Home Assistant core an identity it accepts on any core version, so **ha_auth** mode is expected to work with Spark (live verification in progress). **Legacy** mode remains the verified fallback: a self-hosted authorization server with a static Client ID and Client Secret you paste into Spark's Advanced settings. One limitation of the automatic path: registrations without exactly one stable web redirect origin — multiple origins (as Spark's), loopback-only callbacks (CLI clients), or a mix — sign in normally but cannot refresh; the client must re-authorize when the token expires, which most handle automatically but some surface to the user. See the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) and pick **Gemini Spark** for the full walkthrough.

This page will be updated once ha_auth mode is verified end-to-end with Spark.

### Copilot CLI: remote OAuth works with ha_auth as of component 2.0.0; legacy for older setups

Copilot CLI's remote MCP setup requires an OAuth **Client ID** — its `/mcp add` form won't accept a blank field — and it tries to get one via dynamic client registration. Before component 2.0.0, `ha_auth` mode advertised no registration endpoint, so that registration failed (`MCPOAuthError: Failed to register OAuth client`). As of 2.0.0 the component serves a registration endpoint in `ha_auth` mode, so registration succeeds; legacy mode remains the verified alternative if your setup predates it.

On component 2.0.0 or newer: keep **Authentication mode** on `ha_auth` — Copilot CLI registers automatically and signs in with your Home Assistant account. On older versions (or as a fallback): set **Authentication mode** to `legacy`, restart Home Assistant when the repair prompts you, and copy the generated **Client ID** and **Client Secret** from the Options page (also printed in the Home Assistant log) into Copilot CLI's required fields.

This is the same self-hosted authorization path used for [Google Gemini Spark](#google-gemini-spark-legacy-mode-is-verified-ha_auth-expected-as-of-component-200), which can also use it as a fallback.

### Test ha-mcp without configuring a client

Before setting up a client, you can run a quick smoke test against the public demo server to confirm `uvx` is installed and ha-mcp launches correctly:

```bash
HOMEASSISTANT_URL=https://ha-mcp-demo-server.qc-h.net HOMEASSISTANT_TOKEN=demo uvx ha-mcp@latest
```

This starts ha-mcp in stdio mode connected to the public demo Home Assistant instance. Press **Ctrl+C** to stop. If it launches without errors, your environment is ready — replace the URL and token with your own values to connect to your Home Assistant.

### Keep ha-mcp-web running in the background

To run the ha-mcp HTTP server detached from the terminal so it survives logout:

```bash
HOMEASSISTANT_URL=http://homeassistant.local:8123 \
HOMEASSISTANT_TOKEN=your_long_lived_token \
nohup uvx --from ha-mcp@latest ha-mcp-web > /dev/null 2>&1 &
```

Both variables are required: without them ha-mcp-web exits immediately, and because this command sends output to `/dev/null` the error explaining why is discarded with it. Drop them only if they are already exported in that shell.

`nohup` detaches the process from the terminal and redirects output to `/dev/null`. The trailing `&` sends it to the background. For more robust setups (auto-restart on crash, start on boot), use **systemd** or the **Home Assistant app** instead.

### Docker: changing the port requires updating both places

When running ha-mcp-web in Docker, the port is set in *two* independent places and they must match:

- The **second** number in `-p HOST:CONTAINER` — this is the container-side port Docker listens on
- The `MCP_PORT` environment variable — this is the port ha-mcp binds inside the container

If these differ, the container starts but requests never reach the server. Example using port 9000:

```bash
docker run -d --name ha-mcp \
  -p 9000:9000 \
  -v ha-mcp-data:/home/mcpuser/.ha-mcp \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  -e MCP_PORT=9000 \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web
```

The first number in `-p` (the host port) can be anything — only the second number must match `MCP_PORT`.

---

## Custom Component (ha_mcp_tools)

The **HA-MCP Custom Component** (`ha_mcp_tools`) has two config-entry types under one integration: the **File & YAML services entry** (**HA-MCP File & YAML Tools**) adds the privileged file and YAML-configuration services described below, and the **HA-MCP Server** entry runs the full ha-mcp server in-process inside Home Assistant — now the recommended way to install ha-mcp, working on every Home Assistant installation type (HAOS, Supervised, Container, Core). The **HA-MCP Server** entry is a complete, standalone install that replaces the app, Docker, and uvx/stdio methods — run only one ha-mcp server, never two side by side. See the [in-process server guide](in-process-server.md) for the full walkthrough.

### What is the custom component and why do I need it?

Some tools require a companion custom component installed in Home Assistant. Standard HA APIs do not expose file system access or YAML config editing. This component provides both.

**Tools that require the component:**

- `ha_config_set_yaml` — Safely add, replace, or remove top-level YAML keys in configuration.yaml and package files (automatic backup, validation, and config check)
- `ha_config_get_yaml` — Read the YAML fragment under a key, or find which file defines it (the round-trip parse needs `ruamel`, which the component carries and the server does not)
- `ha_list_files` — List files in the allowed directories
- `ha_read_file` — Read files from the allowed paths
- `ha_write_file` — Write files to the allowed directories
- `ha_delete_file` — Delete files from the allowed directories

The allowed directories are not repeated here — they differ between read and write, are extensible per install, and each tool's own description carries the current list.

Template helper edit backups and restores also require the component, using either its Server entry or File & YAML Tools entry. The tools listed above return an error with installation instructions if the component is missing.

### How do I install it?

**Using HACS (recommended):** open [this HACS repository link](https://my.home-assistant.io/redirect/hacs_repository/?owner=homeassistant-ai&repository=ha-mcp-integration&category=integration), or add it manually: open **HACS** > **Integrations** > three-dot menu > **Custom repositories** > add `https://github.com/homeassistant-ai/ha-mcp-integration` (category: Integration) > **Download**.

After installing, restart Home Assistant. Then open **Settings** > **Devices & Services** > **Add Integration** and search for **HA-MCP Custom Component**, then pick the entry type: **HA-MCP Server** (the in-process server — the recommended install) or **HA-MCP File & YAML Tools** (the file/YAML services described above).

**Manual install:** Copy `custom_components/ha_mcp_tools/` from the [repository](https://github.com/homeassistant-ai/ha-mcp) into your HA config's `custom_components/` directory. Restart Home Assistant, then add the integration as described above.

### Do I also need to enable feature flags?

Yes. The component is required, but the tools are also gated by feature flags for safety:

| Variable | Enables |
|----------|---------|
| `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` | `ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`, `ha_config_get_yaml` |
| `ENABLE_YAML_CONFIG_EDITING=true` | `ha_config_set_yaml` |

The split is deliberate: YAML *reads* sit behind the filesystem flag, and only YAML *edits* need `ENABLE_YAML_CONFIG_EDITING`.

The component itself is installed through HACS — add `homeassistant-ai/ha-mcp-integration` as a custom repository (no feature flag required). An AI agent can drive that install with the generic HACS tools; then add the **HA-MCP File & YAML Tools** entry from **Settings > Devices & Services > Add Integration**.

### Do I still need the app or the webhook proxy if I use the custom component?

**No.** The custom component's **HA-MCP Server** entry runs the whole ha-mcp server inside Home Assistant and is completely independent of the Home Assistant app. For remote access it registers its own built-in Home Assistant webhook, so you do not need the separate Webhook Proxy app either.

The app remains a fully supported alternative on Home Assistant OS and Supervised — pick whichever install you prefer; you never need to run both.

---

## Configuration Options

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Your Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token (or `demo` for demo env) | - | Yes |
| `BACKUP_HINT` | Backup recommendation level | `normal` | No |
| `HA_MCP_DISABLE_SETTINGS_UI` | Set to `1` to skip the localhost settings-page sidecar that stdio installs spawn by default ([details](#how-do-i-open-the-ha-mcp-settings-page)) | - | No |

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
from the *collection* read tools (`ha_search`, `ha_get_overview`). In its default
form this is **noise reduction, not access control** – a hidden entity is still
returned by a direct `ha_get_state` / `ha_get_entity` on its `entity_id`, and
still appears in automation, dashboard, and template content, so do not rely on
the default filter as a security boundary. The opt-in **[Enforce mode](#enforce-mode)**
below turns it into a genuine read barrier: with `"enforce": true`, direct reads
of a hidden entity are concealed and content reads that would surface one are
refused across tool reads, except for the deliberately unrestricted-by-default
`ha_report_issue` diagnostic path described below.

**Default form: reads only – it does not gate control tools.** Without enforce
mode the filter only scopes what the *collection read* tools return. It does
**not** stop an agent from calling a service on a hidden `entity_id`: gating
writes is a separate concern handled by the Tool Security Policies engine (which
matches on a call's arguments), not by visibility. In the default form,
visibility is deliberately read-scoping only, precisely because it is noise
reduction and cannot be a security boundary (content-bearing reads such as
automation and template bodies would leak hidden entities anyway). Enforce mode
changes this by also concealing hidden entities named in a write call's arguments
(see below).

The easiest way to configure it is the **Entity Visibility** tab in the ha-mcp
settings UI (enable toggle, category checkboxes, area/label fields, per-entity
denylist). It reads and writes the same file described below, so either surface
works.

The filter is off until `entity_visibility.json` exists in the ha-mcp data
directory (the same directory as `tool_policy.json`; `/data` in the app) with
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
  "respect_assist_exposure": false,
  "enforce": false,
  "restrict_report_issue": false
}
```

The filter uses a precedence ladder:

- **Hard excludes.** `deny_entity_ids` and concrete `exclude_areas` /
  `exclude_labels` entries always hide a matching entity, even if an allowlist
  also matches it. These settings represent an explicit conflict resolution:
  deny/exclude wins.
- **Broad filters.** `exclude_categories` hides Home Assistant's `diagnostic` and
  `config` categories; unknown values are ignored and surfaced as a `warnings`
  entry on the next read. Set `exclude_hidden: true` to also hide entities marked
  hidden in Home Assistant. These broad filters apply normally when no allowlist
  is active.
- **Allowlist.** The moment any of `allow_entity_ids` / `allow_areas` /
  `allow_labels` is non-empty, the filter enters *restrict* mode: nonmatching
  entities are hidden, including entities added later. A matching entity is
  authorized past the broad category, Home Assistant hidden-state, and Assist
  exposure filters. Hard `deny_entity_ids`, `exclude_areas`, and `exclude_labels`
  conflicts still win. Leave all three allowlist fields empty to disable restrict
  mode.
- **Respect Assist exposure.** With `respect_assist_exposure: true` the filter
  hides entities not effectively exposed to Home Assistant's Assist
  (`conversation`) assistant, mirroring `async_should_expose` (an explicit
  per-entity exposure override wins; otherwise, if the instance exposes new
  entities, the entity's domain and device-class defaults decide). This broad
  filter is not fetched or applied while an effective allowlist remains active.
  If an area/label allowlist degrades open because the entity registry is empty
  and no `allow_entity_ids` are configured, no allow match remains to authorize
  past Assist, so this filter applies again.
  Because HA offers no single "effective exposure" API, the decision is
  reconstructed client-side from two extra websocket reads per search — the set
  of entities explicitly exposed to the assistant (`expose_entity/list`, which
  reports only the *exposed* ones) and the "expose new entities" flag that drives
  the default branch; if either read fails the dimension is skipped with a
  `warnings` note rather than hiding everything. A registry entity's explicit
  override — exposed *or* un-exposed — is read directly from the entity-registry
  `options` the registry list already carries, so an explicit un-expose is honored.
  One residual limit: for an entity that lives only in the state machine (a
  YAML/template entity with no entity-registry entry), HA surfaces it through
  `expose_entity/list` only when it is *exposed*; an explicit un-expose cannot be
  observed there, so such an entity falls to its domain/device-class default and
  stays visible (fail-open).

#### Enforce mode

Set `"enforce": true` (or the **Enforce mode** toggle in the Entity Visibility
tab) to turn the same hidden set into a genuine read barrier applied across tool
reads, not just `ha_search` / `ha_get_overview`. The default exception is
`ha_report_issue`, described below. `enforce` is not a hide dimension — it does
not change *which* entities are hidden, only how strongly the hiding is applied —
so it is inert unless the filter is also `enabled` with at least one active hide
dimension. What it covers:

- **Direct reads are concealed.** A call whose arguments name a hidden entity_id
  exactly (`ha_get_state`, `ha_get_history`, …) is refused *before the tool
  runs* with a canonical `ENTITY_NOT_FOUND`, so the entity's state and
  attributes never flow. Concealment of *existence* is best-effort: per-tool
  not-found shapes vary (a bulk `ha_get_state` normally partial-succeeds, and
  details/suggestions differ per tool), so a caller deliberately comparing
  error shapes may infer that an id is hidden rather than absent. Note this
  also means a bulk read that co-lists one hidden entity is refused as a whole
  — retry without the hidden id to read the rest.
- **Collection reads omit** hidden entities, exactly as they do without enforce.
  In enforce mode this extends to `ha_search`'s configuration-body matches: an
  automation, script, scene, helper, or dashboard record that references a
  hidden entity is omitted from the config results (in the default soft mode
  such records still appear — that is the documented soft-filter behavior).
- **Allowed state reads filter hidden content.** A JSON `ha_get_state` result for
  a visible entity may contain fields, mapping keys, list items, or related
  records that name hidden entities (for example, a person's diagnostic device
  trackers). That content is omitted while the visible entity's own state is
  returned. Because attribute values derive from related entities (the person's
  coordinates come from its trackers), the whole `attributes` mapping is omitted
  when any of its content names a hidden entity. The response receives a
  warning listing the omitted JSON paths and pointing to the Entity Visibility
  settings, so an agent can tell a filtered field from a missing one. Non-JSON
  output, a shape that cannot carry the warning, or any hidden reference left
  after filtering is refused by the normal outbound scan.
- **Other content reads are refused on contact.** An ordinary dashboard config,
  template result, automation/script body, trace, log, or file read whose output
  would surface a hidden entity_id is refused with a generic
  `ENTITY_VISIBILITY_ENFORCED` error that never names the matched id.
- **Writes naming a hidden entity are concealed too.** The inbound argument scan
  applies to *every* tool, including service calls: a `ha_call_service` targeting
  a hidden entity_id is concealed as not-found, so an agent cannot confirm the
  entity by trying to control it.

What it deliberately **refuses** (their output cannot be text-scanned): sandbox
code execution via `ha_manage_custom_tool` (`code` / `run_saved` — pure
`list_saved` stays allowed) and screenshot/pixel output
(`ha_get_dashboard_screenshot`, `ha_config_get_dashboard` with
`include_screenshot`, or `ha_config_set_dashboard` with `return_screenshot`).

One image surface is deliberately **exempt**: `ha_get_camera_image`. A camera
the filter does not hide returns physical-world imagery — a photograph, not a
rendering of Home Assistant entity data — so its frames are not gated (a hidden
camera is concealed like any other entity). The residual case is a visible
camera whose view happens to include a display showing a hidden entity's state;
if a camera can see something sensitive, hide the camera too (denylist or its
area).

A second surface is deliberately **exempt by default**: `ha_report_issue`.
While `"restrict_report_issue": false`, it bypasses both visibility scans on
every call — not only during a registry failure — and can return diagnostic
fields such as core, app/add-on, recent, and startup logs that contain hidden
entity_ids. This keeps the troubleshooting path available when visibility
configuration or Home Assistant registry inputs are the problem. Set
`"restrict_report_issue": true` to scan and refuse it like other tool reads.

Except for that default diagnostic escape hatch, enforce mode **fails closed**:
if the entity registry (or the config file itself) cannot be loaded, the server
falls back to the last good read from this session — and with none available,
tool calls are refused rather than risk leaking a restricted entity. If no
config can be read, `ha_report_issue` follows its safe unrestricted default;
if a last-good config opted it in, it fails closed too. The hidden set is cached
for ~30s, so an area/label membership change in Home Assistant can take up to
that long to take effect for the area/label dimensions (a config edit in the
settings UI applies on the next call).

Because refuse-on-contact applies to the *whole* hidden set, broad hide
dimensions make refusals frequent: with the default `diagnostic`/`config`
category excludes still active, any log, automation, or dashboard read that
mentions a diagnostic entity is refused wholesale. Enforce mode works best with
a *targeted* deny — the private areas, labels, or entity_ids you actually need
concealed — rather than broad decluttering dimensions.

**Honest residual limits.** This is a strong barrier against *incidental*
exposure, not a cryptographic guarantee. A Jinja template (or code) that *derives*
a hidden entity's state without ever naming its entity_id — e.g.
`{{ states | selectattr('state','eq','on') | list | count }}` — cannot be caught
by a text scan. Treat enforce mode as robust protection against an agent stumbling
onto hidden entities, not as a boundary against an adversarial prompt author who
is deliberately trying to exfiltrate a hidden entity's state.

`version` drives optimistic-concurrency for the settings UI (it bumps
on each save so two tabs can't clobber each other); when hand-editing the file,
leave it as-is. The config is read live per request, so edits apply on the next
call. A missing file leaves the filter off; an *invalid* one leaves the filter
off for search/overview (with a `warnings` note) while enforce-mode safety falls
back to the session's last good config — with none, tool calls are refused until
the file is fixed (see *Enforce mode* above). When the filter is enabled but the
registry read degrades, registry-derived dimensions (categories, hidden-state,
areas, labels, Assist) are skipped with a `warnings` note; `deny_entity_ids` and
`allow_entity_ids`, which need no registry data, still apply.

### A rule gates one tool, not one capability

Policies apply to individual tools. Other tools may perform the same action,
and a rule does not follow the capability across them: requiring approval for
`ha_call_event` does not restrict event firing through `ha_call_service`,
whose raw `ws_command` escape hatch reaches the same WebSocket command.

That is deliberate — gating one tool must not silently withdraw another — so
write the rules for every tool that reaches what you want held. One partial
safety net exists: an unmatched `ws_command` call is held whenever the policy
has any rule targeting `ha_call_service` or `*`, so the escape hatch cannot
slip past a policy that already watches that tool.

### Getting notified when a tool call is waiting for approval

A rule in **Tool Security Policies** holds the call and shows it in the
settings UI, which only helps while that tab is open. Every held request is
also announced on the Home Assistant event bus as
`ha_mcp_approval_requested`, so you can build your own notification around
it:

```yaml
automation:
  - alias: Notify me about pending ha-mcp approvals
    triggers:
      - trigger: event
        event_type: ha_mcp_approval_requested
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Approval needed: {{ trigger.event.data.tool_name }}"
          message: "{{ trigger.event.data.args }}"
```

The event data carries `token`, `tool_name`, `args`, `created_at` and
`expires_at`, plus `matched_rule` whenever a rule matched the call. A policy
can also gate a call no rule matched — through one of the fail-safes for raw
WebSocket commands and for selector-based bulk calls — and then there is no
rule to name and the key is absent.

Two things to know about `args`: each value is capped, so a long one is
shortened with an `omitted` marker and the settings UI stays the place to
read it in full; and the event bus reaches every listener, so treat those
arguments as you would any other bus traffic. The cap is a size limit, not
a redaction — a short argument is broadcast exactly as it is.

The announcement is one best-effort attempt per held request, not a
delivery guarantee. If firing the event fails or times out it is logged as
a warning and the request still waits in the settings UI; nothing re-sends
it. A retry of the same call joins the same held request and is not
announced a second time — one request, one notification. Only a request
that is replaced by a new one is announced again, with the new token; the
token from the previous event is dead by then.

A selector-based `ha_bulk_control` request is announced with `single_use:
true` and **no** `expires_at`. It is bound to the one call that created it
and is gone once that call stops waiting, which is well before the policy's
TTL — approving it later does nothing, and the agent has to call the tool
again.

Every other request stays approvable for the policy's `approval_ttl_minutes`
even after the blocked call gave up waiting after `wait_seconds`: approve it
in the tab and the agent's next identical call goes through, which is what
the error tells the agent to do.

If no event arrives at all, check the token the server authenticates with:
Home Assistant only accepts `POST /api/events/<type>` from an admin user, so
a standalone install running on a non-admin long-lived token gets a 403 that
goes to the server log and nowhere else. The embedded component provisions
its own admin token, so it is not affected.

Approving happens in the Tool Security Policies tab by default. Answering
from an automation is possible too, behind a switch and a PIN — see the next
question. (`ha_dev_manage_server` can also decide a pending request, but only
where two separate settings are both on: developer mode, and
`dev_tools_security_policy_access`. That is a testing tool and it says so.)

### Approving or denying from a notification instead of the settings tab

Off by default. On the **Tool Security Policies** tab, set an approval PIN
and switch on *Allow approve/deny from Home Assistant events*. A pending
request is then decided by firing `ha_mcp_approval_response` with the token
from the request event, a decision and the PIN:

```yaml
script:
  approve_ha_mcp_request:
    fields:
      token:
        description: The token from the ha_mcp_approval_requested event
    sequence:
      - event: ha_mcp_approval_response
        event_data:
          token: "{{ token }}"
          decision: approve        # or: deny
          pin: !secret ha_mcp_approval_pin
```

Call that script from whatever answers for you — a notification action, a
dashboard button, Developer Tools — passing the token the request event
carried. Keep the PIN in `secrets.yaml` rather than inline. The server does
not care how the event was fired, which is exactly the limitation below.

**What the PIN does and does not protect.** Home Assistant cannot tell an
event fired by your automation from one fired by an AI agent: the agent can
author an automation of its own, and an automation-fired event carries
neither a distinguishing origin nor a user. The PIN is therefore the only
thing separating them, and an agent with enough access can obtain it — by
asking you, or by writing an automation that reads it out of a response
event you fire. Switching this on accepts that; leaving it off means no
event can decide a request — an agent then has no way to approve its own
requests over the bus. (It says nothing about the developer tool above,
which stays available wherever developer mode and
`dev_tools_security_policy_access` are both on.) The PIN is stored as a
salted hash and is set only through the settings UI — no MCP tool takes it,
returns it, or can write the file it lives in. Five wrong PINs within five
minutes close the channel for the rest of that window; the Pending list in the
settings UI, which the server itself serves, keeps working throughout. (The
stdio sidecar's settings page sets the PIN and edits the policy, but cannot
list or decide pending approvals: those live in the server process it cannot
reach.) The PIN is kept out of the policy document on purpose, so no surface
that reads or writes policy — the settings UI, `ha_manage_security_policy`, a
version-conflict error body — carries it. The file itself is mode 0600 and
holds only the digest; on an embedded install it lives under the `.ha_mcp`
folder of your configuration directory, where the component's non-overridable
deny floor blocks its filename on read, write and deletion, and keeps it out
of directory listings. Adding that folder to the component's **Extra file
paths** setting therefore cannot hand a tool the digest — but it does grant
read *and* write over everything else in there, which is its own decision to
make.

Removing the PIN switches the feature off with it. Events that arrive
without a matching PIN are refused and logged at WARNING. An event that
arrives while the feature is off is refused too, but logged at INFO — and
while the switch has been off for every request announced so far, nothing
has subscribed to the response event at all, so such an event is never even
received. Either way the request
stays pending and decidable in the tab.

### Finding out what became of a response

Every response event the server *receives* is answered on the bus with
`ha_mcp_approval_result`:

```yaml
automation:
  - alias: Tell me whether my approval landed
    triggers:
      - trigger: event
        event_type: ha_mcp_approval_result
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: >-
            {{ trigger.event.data.decision }} →
            {{ 'applied' if trigger.event.data.applied else
               trigger.event.data.reason }}
```

The payload carries `token`, the `decision` that was asked for, whether it
was `applied`, and a `reason`. `tool_name` is included when the request is
still known — an expired or invented token names nothing, so the field is
absent rather than guessed. The reason is one of `applied`, `expired`,
`unknown_token`, `already_decided`, `wrong_pin`, `no_pin`, `pin_not_set`,
`pin_unusable`, `rate_limited`, `feature_off` or `policy_unreadable`: short
tokens, so an automation can branch on them without matching prose.

Four things it deliberately does not do.

It never carries the PIN or its digest, in any form — not even a hint about
how close a wrong one was.

It makes no claim about **who** responded. The bus cannot tell a response
your automation fired from one an agent wrote itself, so nothing in the
payload pretends it can. That limitation is the same one the PIN exists to
bound, and it does not change here.

`applied: true` means the held call was released to run. It is **not** a
report that the tool then succeeded — that is the tool's own business and
has its own result.

And silence is **not** a refusal. A result is produced only for a response
the server actually received, which means the feature was on and the
channel was open. Fire a response while the feature is off, or before
anything has subscribed, and there is no result event because there was no
recipient. An automation that treats a missing result as a denial will be
wrong in exactly the case where you most need to open the settings tab —
so use the reason when one arrives, and the tab when none does.

---

## Feedback & Help

We'd love to hear how you're using ha-mcp!

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Share how you use it, ask questions, show off your automations
- **[GitHub Issues](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs or request features
- **[Home Assistant Forum](https://community.home-assistant.io/t/brand-new-claude-ai-chatgpt-integration-ha-mcp/937847)** — Community discussion thread
