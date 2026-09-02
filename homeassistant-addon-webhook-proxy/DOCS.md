# Webhook Proxy for HA MCP - Documentation

Remote access proxy for the Home Assistant MCP Server app (add-on) via webhooks.

## About

This app enables remote access to your HA MCP Server through any reverse proxy — Nabu Casa, Cloudflare, DuckDNS, nginx, or any other. It does **not** run its own MCP server; instead it discovers your existing MCP Server app (stable or dev) and proxies requests to it via a Home Assistant webhook.

> **Using the HA-MCP custom component (in-process server)?** You do not need this
> app — the component has its own built-in webhook for remote access. This
> proxy is for the **MCP Server app** (it can also front another external
> server via the `mcp_server_url` option).

## Only one Webhook Proxy flavor runs at a time

This add-on and its development counterpart, **Nabu Casa - Webhook Proxy for HA MCP
(Dev)**, cannot run at the same time — they would collide over Home Assistant's root
OAuth `/authorize` and `/token` routes. If you start this add-on while the **(Dev)**
add-on is running, it refuses to start (a clear error in the add-on log plus a Home
Assistant notification). Stop the (Dev) add-on first; the notification clears
automatically on the next clean start.

## Prerequisites

- **Home Assistant MCP Server** app must be installed and running (stable or dev channel)
- For Nabu Casa auto-detection: active Nabu Casa subscription with remote UI enabled
- For other setups: a working reverse proxy pointing at your HA instance

## Setup

1. **Install this app (add-on)** from the App store
2. **Start the app** — on first run it will install the integration and create a notification asking you to restart Home Assistant
3. **Restart Home Assistant** (Settings > System > Restart) — the app detects the restart and automatically finishes setup
4. **Copy the remote URL** from the app logs:
   ```
   MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx
   ```
5. **Paste the URL** into your MCP client (Claude Desktop, Claude.ai, Open WebUI, etc.)

### Connecting from Claude.ai (web)

1. In Claude.ai, go to **Settings → Connectors → Add custom connector**, give it a name, paste the remote URL, and click **Add**.
2. **Click _Connect_ on the new connector.** This step is required — adding the connector does not connect it. With OAuth enabled this opens the consent page; click **Allow** to finish. (With OAuth off there is no consent page and it connects directly.)
3. Claude.ai may briefly show *"Couldn't reach the MCP server"* — this is often a harmless artifact of the initial handshake. Check whether the connector actually shows as connected before assuming it failed.

> **Reachability check:** Claude.ai connects from Anthropic's servers, not from your computer — so the URL must be reachable from the public internet, not just your LAN. If a connection won't establish, open the remote URL on your **phone with Wi-Fi turned off** (cellular only). If it doesn't load there, the URL isn't publicly reachable (a DNS, port-forward, TLS, or reverse-proxy problem) and Claude.ai can't reach it either — fix that first.

> **Tailscale Funnel: use port 443.** Claude.ai's backend does not reliably
> reach a Funnel published on a non-standard HTTPS port (`8443`, `10000`) —
> the connector fails identically in every auth mode (no auth, `legacy`,
> `ha_auth`) and no request from Anthropic's range ever reaches your logs. Use the
> Tailscale app's built-in **Share Home Assistant with Serve or Funnel**
> option, which publishes on the standard port 443, and build the connector
> URL on that hostname (same webhook path).

> **Recreate the connector when OAuth or the URL changes.** Claude.ai binds an
> authentication mode to a connector when you add it, and caches it. If you
> later **turn OAuth on or off**, or the **webhook URL changes** (you rotated
> it, or reinstalled the app — which generates a new URL), the existing
> connector keeps using the old mode/URL and tool calls fail (often
> `invalid client id` on the consent page, or a silently dead endpoint).
> **Delete the connector in Claude.ai and add a fresh one** with the current
> URL (and current OAuth Client ID/Secret, if OAuth is on). This is required
> even when going from OAuth on → off.

> **Note:** If something doesn't seem to work after restarting HA, try restarting the app as well.

## Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `remote_url` | Your external URL (auto-detects Nabu Casa if blank) | `""` |
| `mcp_server_url` | Full MCP server URL override (auto-detects if blank) | `""` |
| `mcp_port` | MCP server port used during auto-discovery | `9583` |
| `enable_oauth` | **Beta.** Require OAuth 2.1 on the webhook URL | `false` |
| `oauth_mode` | **Beta.** Bearer-protection mode: `ha_auth` or `legacy` | unset |
| `oauth_client_id` | OAuth Client ID (auto-generated if blank) | `""` |
| `oauth_client_secret` | OAuth Client Secret (auto-generated if blank) | `""` |
| `regenerate_oauth_creds` | One-shot: wipe stored OAuth creds and generate fresh ones on next start | `false` |
| `debug_logging` | **Beta.** Log every inbound request to the Home Assistant log to confirm a client is reaching the server | `false` |

> The OAuth options are hidden by default. Toggle **Show unused optional configuration options** at the bottom of the app's Configuration tab to reveal them. `debug_logging` is shown on the main Configuration page (it is not one of the hidden options).

### Auto-detection

When `mcp_server_url` is left blank (recommended), the app automatically:

1. Finds the running MCP Server app (tries stable `ha_mcp` first, then dev `ha_mcp_dev`)
2. Gets its container IP address from the Supervisor API
3. Discovers the secret path from the app's options or logs
4. Constructs the target URL: `http://<ip>:<port>/<secret_path>`

### Manual URL override

If auto-detection doesn't work for your setup (e.g. non-standard port, custom networking), set `mcp_server_url` to the full MCP server URL:

```
http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw
```

### Remote URL

- **Nabu Casa subscribers**: Leave `remote_url` blank — auto-detected from cloud storage
- **Cloudflare/DuckDNS/nginx**: Set `remote_url` to your external URL (e.g. `https://ha.example.com`)
- **Tailscale Funnel**: Use the Tailscale app's built-in **Share Home Assistant with Serve or Funnel** option, which publishes on port 443, and set `remote_url` to that hostname — Claude.ai cannot reliably reach a Funnel published on an alternate port such as `8443`

Your external URL must point directly at Home Assistant — opening it in a browser should land on your HA login page — and it must **not** contain a port such as `:8123` (or any other port). If the connect URL carries a port, remote MCP clients such as Claude will fail to reach it even though it loads fine in your own browser.

### Authentication

By default the webhook URL is **unauthenticated** — possession of the URL is the only credential, and the URL functions as a shared bearer secret. The MCP server exposed through the proxy includes powerful Home Assistant control tools, so you should **treat the URL like a password**.

#### Treat the URL as a secret (default mode)

- **Don't share the URL** in screenshots, chat transcripts, log paste-bins, or public configs. Mask the path segment after `/api/webhook/` if you need to share anything.
- **Avoid copying it into untrusted clients.** Anyone with the full URL can call your MCP tools.
- **Rotate immediately if you suspect it leaked.** See below.

#### Rotating the webhook URL

The URL stays the same across app restarts because the webhook ID is persisted at `/data/webhook_id.txt` inside the app container. To rotate:

1. **Stop** the Webhook Proxy app.
2. **Delete** the persisted webhook ID file. Open the app's filesystem (e.g. via the SSH/Terminal app or a file browser) and remove `/data/webhook_id.txt` for the proxy app. (Stopping then uninstalling and reinstalling the app also achieves this.)
3. **Start** the app. A fresh webhook ID and URL are generated on first launch.
4. **Copy the new URL** from the app logs and paste it into your MCP client(s). The old URL is now dead.

The old webhook ID is not retained anywhere on disk after the file is deleted, and the integration's previous registration is dropped when the app stops.

#### Enable OAuth (Beta) for stronger protection

If you want a real auth layer on top of the URL secret, turn on **Enable OAuth (Beta)**. It is OFF by default; leaving it off keeps the webhook working with no auth check. The proxy still serves a none-mode OAuth compatibility surface so clients that insist on OAuth discovery can connect, but its token is cosmetic and grants no access by itself.

The proxy has three live OAuth behaviors. When OAuth protection is enabled, choose one of the two protected modes with **OAuth Mode (Beta)**:

- **None mode (OAuth protection off)** — the secret webhook URL remains the only credential. OAuth discovery, stateless registration, and an invisible public-PKCE exchange are available for client compatibility, but the resulting token is not checked by the webhook.
- **`ha_auth` (recommended, the default for a first-time enable)** — Home Assistant itself is the authorization server. You sign in with your Home Assistant account and **leave the connector's OAuth fields blank** (Claude.ai auto-detects **Always required** + **Use Anthropic's hosted client metadata** — keep both). No Client ID or Client Secret, works with any hostname/URL, and no Home Assistant restart is needed to enable or disable it.
- **`legacy` (deprecated)** — the previous flow, where the app generates a Client ID + Secret you paste into the connector.

> **Upgrading? Your OAuth setup is not changed.** If you already used the legacy flow (you set a Client ID/Secret, or the app stored one), leaving **OAuth Mode** unset keeps you on **legacy** — nothing breaks. New/first-time enables default to **ha_auth**. Switching modes is an explicit action (set **OAuth Mode**) and, because Claude.ai binds the auth mode per connector, requires **deleting and re-adding your MCP connector**.

##### Recommended: sign in with Home Assistant (`ha_auth`)

1. Toggle **Show unused optional configuration options** at the bottom of the Configuration tab.
2. Set **Enable OAuth (Beta)** to on. Leave **OAuth Mode** unset (or set it to `ha_auth`) for a first-time enable.
3. Save and **restart the app**. (No Home Assistant restart is required for this mode.)
4. In your MCP client, add the connector with the webhook URL and **leave the OAuth Client ID and Client Secret fields blank**. When you connect, you sign in with your Home Assistant account and approve access — that is the whole flow.
   - **Claude.ai:** if its UI insists on a Client Secret, any value works — Home Assistant ignores it.
5. **Revoking access:** open your Home Assistant profile and remove the session/refresh token for the connector (Settings → your user → Security). No app action needed.

Why this mode is host-agnostic: the app serves the OAuth discovery and protocol endpoints itself, so they work on any hostname even when Home Assistant's own metadata would not. Its scoped handlers translate validated CIMD or DCR client identities, then redirect or relay into Home Assistant core for the actual login and token issuance.

##### Legacy mode (client id + secret)

Set **OAuth Mode** to `legacy` (or leave it unset if you are upgrading an existing legacy setup). The app runs a minimal OAuth 2.1 authorization-code-with-PKCE flow on the same webhook URL, discoverable from a 401 response, with a one-screen consent page.

1. Toggle **Show unused optional configuration options** at the bottom of the Configuration tab.
2. Set **Enable OAuth (Beta)** to on and **OAuth Mode** to `legacy`. Leave **OAuth Client ID** and **OAuth Client Secret** blank — the app will generate strong values for you on first start.
3. Save and **restart the app**. Legacy mode requires a full **Home Assistant** restart to take effect (a Repair with a restart button appears); disabling does not.
4. Open the app log and copy the displayed Client ID and Client Secret. Both values are printed in plaintext exactly as Claude.ai needs them — copy them straight from the log:
   ```
   OAuth (Beta) is ENABLED for this URL (legacy mode).
     OAuth Client ID:     hamcp-1a2b3c4d5e6f7890abcdef1234567890
     OAuth Client Secret: kX9pQ4mZ2vL8nR3sT6uW1yA5cB7dF0gH...
     Paste both into the OAuth fields of your MCP client's
     connector setup (Claude.ai: OAuth client → Use your own OAuth client).
   ```
5. In your MCP client, configure the OAuth fields:
   - **Claude.ai:** when adding the connector, on the second wizard step keep **Authentication: Always required**, pick **OAuth client: Use your own OAuth client**, and paste the Client ID and Client Secret. Claude.ai completes the rest automatically (consent screen → token exchange → bearer token).
   - **Other clients:** configure the same Client ID + Client Secret in the client's OAuth settings if it supports OAuth 2.1 with manual client registration.

The generated values are persisted at `/data/oauth_creds.json` inside the app, so they stay the same across restarts.

**Rotating the legacy credentials** — three options:

1. **Pick your own new values:** type new strings into the Client ID and Client Secret fields, save, restart the app. Your values overwrite the persisted file. Update your MCP client to match.
2. **Get fresh random values via the UI** (no filesystem access needed): turn on **Regenerate OAuth Credentials on Next Start** in the app configuration, save, restart. The app wipes the stored creds, generates a fresh pair, prints them in the log, and flips the regenerate toggle back to off. Update your MCP client to match.
3. **Get fresh random values manually:** stop the app, delete `/data/oauth_creds.json` (e.g. via SSH/Terminal app), start the app. Equivalent to option 2 but requires filesystem access.

**To disable OAuth protection:** set **Enable OAuth (Beta)** back to off and restart **Home Assistant** (Settings → System → Restart) — restarting the app alone does not drop the OAuth views, see the restart note below. The webhook returns to unauthenticated URL-as-secret behavior. The none-mode compatibility endpoints remain available, but their cosmetic token is not required by the webhook.

**OAuth endpoints:**

All three behaviors advertise proxy-owned endpoints under `/api/mcp_proxy/oauth`, so a cached client configuration remains on the proxy across mode switches:

- `/api/mcp_proxy/oauth/protected-resource` — RFC 9728 protected-resource metadata (`legacy` and `ha_auth` only: in none mode this fixed route returns 404, because its metadata would reveal the credential-bearing webhook URL — none-mode clients use the webhook-ID-scoped `.well-known` route instead)
- `/api/mcp_proxy/oauth/authorization-server` — RFC 8414 authorization-server metadata
- `/api/mcp_proxy/oauth/authorize` — mode-dispatched authorization endpoint
- `/api/mcp_proxy/oauth/token` — mode-dispatched token endpoint
- `/api/mcp_proxy/oauth/register` — stateless DCR endpoint, advertised in `ha_auth` and none mode
- `/api/mcp_proxy/oauth/revoke` — RFC 7009 revocation endpoint, advertised and served in `ha_auth` mode only (404 elsewhere)

The same authorize/token URLs behave according to the active mode:

- **None mode:** accepts any valid HTTPS or RFC 8252 loopback redirect, auto-approves without a page, and issues a cosmetic token. DCR registrations advertise only the authorization-code grant.
- **`ha_auth`:** validates CIMD or signed DCR identities before sending the browser/token exchange into Home Assistant core. DCR advertises refresh for every registration: a forwarded token response comes back with its refresh token wrapped in a signed envelope naming the identity core bound it to, so loopback-callback and multi-origin clients refresh without re-authorizing. The proxy also fronts token revocation on its own scoped revocation endpoint, so a wrapped refresh token is unwrapped before Home Assistant sees it and is really revoked. Revocation accepts a wrapped token even when its signature no longer verifies — after the signing key rotates, which is what removing and re-adding the integration does — because possession of a token is the only authorization revoking it needs, and Home Assistant's own revocation endpoint is anonymous and idempotent. Sessions authorized before this version carry no envelope: they re-authorize once, then refresh normally.
- **`legacy`:** serves the existing consent and credentialed token flow on the scoped URLs. The host-root `/authorize` and `/token` routes remain compatibility aliases but are not advertised; legacy does not advertise DCR.

**Hosted Claude environment requirements:** Hosted Claude surfaces reach your server from Anthropic's backend, not from your browser. Three environment rules apply that no server-side setting can work around:

1. **Port 443 only (hosted Claude connectors).** The connector URL must use standard HTTPS with no explicit port. Anthropic's backend only connects on port 443, so `https://ha.example.com:8123/...` is not reachable from hosted Claude connectors — put a reverse proxy or port-forward on 443 in front and keep 8123 internal.
2. **Allowlist Anthropic's egress range.** Backend traffic originates from `160.79.104.0/21`. A WAF, geo-block, or bot filter (Cloudflare "AI crawl" rules included) that blocks that range breaks registration, token exchange, and the post-OAuth handshake even though your browser works fine.
3. **Answer fast.** Claude waits at most 10 seconds for discovery, registration, and token responses (30 for refresh) per [Anthropic's connector authentication documentation](https://claude.com/docs/connectors/building/authentication). Slow reverse proxies or cold paths cause intermittent connection failures.

**Notes (legacy mode):**

- Tokens are HMAC-signed and stateless — they survive HA restarts. Access tokens expire after 1 hour; refresh tokens after 30 days.
- Rotating the Client ID invalidates all outstanding tokens (the client_id is part of the token's signed payload).
- The signing key is generated once and persisted at `/config/.mcp_proxy_oauth_secret`. Delete that file to invalidate every token in one shot.

**Beta status:** OAuth protection is Beta in both protected modes; the URL-as-secret mode (default) is the stable, documented path. `ha_auth` delegates authentication and token issuance to Home Assistant's own OAuth (validated live against claude.ai); `legacy` is implemented against the [MCP 2025-06-18 spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization). Real-world MCP-client coverage varies, so treat OAuth as opt-in until tested with your client, and report problems on GitHub.

### Legacy end-to-end flow (what happens when Claude.ai connects)

When OAuth is enabled and you paste the webhook URL + Client ID + Client Secret into Claude.ai's connector setup, here's what happens after you click **Connect** on the connector:

1. **Claude.ai's browser session** is redirected to `https://<your-host>/api/mcp_proxy/oauth/authorize?response_type=code&client_id=...&redirect_uri=...&code_challenge=...&code_challenge_method=S256&state=...&resource=https://<your-host>/api/webhook/<id>`.
2. The app serves a **consent page** (Allow / Deny) showing the redirect destination so you can verify it's Claude.ai's callback URL before proceeding.
3. You click **Allow** → the app issues a one-time auth code and redirects the browser back to Claude.ai's callback (`redirect_uri`).
4. Claude.ai's backend exchanges the auth code at `https://<your-host>/api/mcp_proxy/oauth/token` using the configured Client ID and Client Secret (Basic auth or form body) plus the PKCE `code_verifier`. The app validates all of these, then issues:
   - An **access token** (1-hour HMAC-signed bearer)
   - A **refresh token** (30-day HMAC-signed bearer)
5. Claude.ai stores the tokens. From then on, every MCP request includes `Authorization: Bearer <access-token>`. The webhook handler validates the bearer; expired tokens are refreshed automatically using the refresh token.

### Legacy threat model and the role of the Client Secret

The scoped `/api/mcp_proxy/oauth/authorize` consent page is reachable without being logged into Home Assistant or Nabu Casa. **This is intentional and matches how mainstream OAuth servers work** — the consent page must be reachable by the browser session being redirected from the OAuth client (Claude.ai), which has no Home Assistant credentials of its own.

What stops an attacker who can reach the consent page from gaining access:

- Clicking **Allow** on a maliciously-crafted authorize URL only mints a one-time auth code bound to the attacker's `redirect_uri` and PKCE `code_challenge`.
- That code is **useless without the OAuth Client Secret**, which is required at the scoped `/api/mcp_proxy/oauth/token` endpoint to complete the exchange.
- PKCE binds the code to the original `code_verifier`, which only the legitimate client (Claude.ai) generated.

**This means: the OAuth Client Secret is the actual security boundary.** Treat it like a password.

**Where the Client Secret lives:**

- The app log on each start (plaintext for copying into Claude.ai)
- The app's persistent storage at `/data/oauth_creds.json`
- Claude.ai's backend (after you paste it into the connector setup)
- Anywhere you've recorded it (password manager, etc.)

**Keep the Client Secret safe:**

- Don't share the app log publicly without redacting the OAuth Client Secret line.
- Don't paste it into chat transcripts, screenshots, support threads, or public configs.
- If you suspect it has leaked, **rotate it immediately** using one of the three rotation methods above. After rotation, the old Client Secret stops working — any tokens previously issued will fail at refresh, forcing the client to re-do the OAuth flow with the new credentials.
- If you can't tell whether it leaked but want a clean slate (e.g., after sharing logs for debugging, after a migration), rotating proactively is cheap: flip **Regenerate OAuth Credentials on Next Start**, restart, paste the new credentials into Claude.ai. Takes ~30 seconds.

### Debugging connections (log inbound requests)

If a client (e.g. Claude.ai) can't connect and you can't tell whether its requests are even reaching Home Assistant, turn on **Log inbound requests** (the `debug_logging` option on the main Configuration page) and **restart the app**.

When it's on, every request that hits the webhook is logged to the **Home Assistant log** (along with Home Assistant's own webhook lines, which are otherwise hidden — that is what distinguishes a request that never arrived from one that arrived for an unregistered id) (requests reach Home Assistant directly rather than passing through the app process). View them at **Settings → System → Logs** (or filter for `mcp_proxy`). The same lines are also **mirrored into this app's own log**, so you can watch them on the app's Log tab without leaving the app page. Each line shows the method, a masked webhook path, the source address, whether an `Authorization` header was present, and the upstream response status:

```
MCP Proxy [inbound]: POST /api/webhook/mcp_3e... from 203.0.113.4 (Authorization header: present)
MCP Proxy [inbound]: -> upstream responded 200 (text/event-stream)
```

How to read it:

- **You see inbound lines** → the client is reaching the server; the problem is downstream (auth, the client's config, or the MCP server itself).
- **You see `Received message for unregistered webhook`** (Home Assistant's own line, surfaced only while this toggle is on) → the request DID arrive, but nothing is registered for that webhook id, so Home Assistant answered an empty `200` without ever reaching this app. Usually a stale URL: compare the id in the URL your client uses against the `webhook endpoint = /api/webhook/mcp_…` line this app logs at startup, and restart Home Assistant if a restart Repair is pending.
- **You see nothing at all** → the request never arrived. The problem is network reachability — the public URL, DNS, TLS, or your reverse proxy — not this app. See the reachability check under [Setup](#connecting-from-claudeai-web).

Turn it back off for normal operation (restart the app after changing it).

## How it works

1. The app installs a lightweight `mcp_proxy` custom integration into Home Assistant
2. By default this integration registers an **unauthenticated** webhook endpoint (`/api/webhook/<id>`) — the URL itself is the shared secret
3. When a request hits the webhook, it is proxied to the MCP server app (after a bearer-token check, if **Enable OAuth** is on)
4. The app stays alive with a periodic health check loop

The webhook bypasses the ingress session cookie requirement that external MCP clients cannot provide.

## Troubleshooting

### "No running MCP app found"

The main MCP Server app is not running. Install and start it first:
- Settings > Apps > Home Assistant MCP Server > Start (Settings > Add-ons before Home Assistant 2026.2)

### "Could not discover secret path"

The app could not find the secret path. Options:
1. Check that the MCP Server app has started successfully and shows a URL in its logs
2. Set `mcp_server_url` manually in this app's configuration

### "MCP server unreachable"

The health check cannot reach the MCP server. Check:
1. The MCP Server app is still running
2. No network/firewall issues between apps
3. The port matches (default 9583)

### Integration not loading

If the `mcp_proxy` integration doesn't appear in Settings > Devices & Services:
1. Restart Home Assistant (Settings > System > Restart)
2. The app will start automatically and retry setup

### Persistent errors, especially OAuth "Invalid client id"

If the proxy keeps returning the same error — most notably **"Invalid client id"**
on the OAuth consent page even though you pasted the correct Client ID — **fully
restart Home Assistant** (Settings → System → Restart).

The OAuth provider's HTTP views are bound into Home Assistant's HTTP layer when the
integration first loads, and Home Assistant can't re-register or drop them on a
config reload. So changes that come from **toggling OAuth on/off, regenerating
credentials, or reinstalling the app** don't take effect until a full HA
restart — *reloading the integration or restarting the app is not enough.* (The
webhook endpoint itself is re-registered on every reload, so it's specifically the
OAuth views that need the restart.) After the restart, re-add the Claude.ai
connector with the current URL (and current Client ID/Secret, if OAuth is on).

### Claude.ai says "Couldn't reach the MCP server"

Three cases:

1. **It actually connected.** Claude.ai sometimes shows this during the initial handshake even though the connector ends up working. Check whether the connector shows as connected before assuming failure.
2. **It genuinely can't reach the URL.** Claude.ai connects from Anthropic's servers, so the URL must be reachable from the public internet — not just your LAN. Open the remote URL on your **phone with Wi-Fi off** (cellular): if it doesn't load, the URL isn't publicly reachable (DNS / port-forward / TLS / reverse-proxy) and Claude.ai can't reach it either. To confirm whether requests are arriving at all, enable **Log inbound requests** (see [Debugging connections](#debugging-connections-log-inbound-requests)).
3. **The URL is reachable, but your proxy blocks AI clients.** If the URL loads in a browser but the LLM still can't connect, your reverse proxy is filtering the AI client — see [Cloudflare users: browser works but the LLM can't connect](#cloudflare-users-browser-works-but-the-llm-cant-connect) below (Cloudflare's "Block AI training bots" setting and geo / country blocking are the usual causes).

> **Note:** A connection working in Claude Code or a local browser but **not** in Claude.ai web is the classic signature of this — those reach your box over the LAN, while Claude.ai reaches it from the public internet.

### Cloudflare users: browser works but the LLM can't connect

If your remote URL goes through Cloudflare and the URL loads fine in your browser but your AI/LLM client still can't connect, check these two settings:

**1. "Block AI training bots"** — the most common connection issue for Cloudflare users. Cloudflare blocks requests from AI/LLM clients by default. To disable it:

1. Log in to [Cloudflare](https://dash.cloudflare.com)
2. In the left sidebar, click **Domains**, then click **Overview**
3. Click on the domain you use for connecting to Home Assistant
4. On the right side of the page, find **"Control AI Crawlers"**
5. Under **"Block AI training bots"**, open the dropdown
6. Select **"do not block (allow crawlers)"**

![Cloudflare AI Crawlers Setting](https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg)

See [#783](https://github.com/homeassistant-ai/ha-mcp/issues/783) for more details.

**2. Geo / country blocking** — applies to any reverse proxy, not just Cloudflare. Most AI/LLM services connect from US-based cloud infrastructure, so if you block US IP addresses (or only allow your own country), your client cannot connect. Allow your AI provider's IP ranges — Claude.ai connects from Anthropic's network, `160.79.104.0/21` (see [Anthropic's IP ranges](https://platform.claude.com/docs/en/api/ip-addresses)). Your proxy's access logs will show the blocked attempts.

## Disabling / Uninstalling

- **Stopping** the app is safe — the webhook URL stays the same and resumes working when the app is restarted
- **Reinstalling** the app always changes the webhook URL. Uninstalling wipes the app's `/data` (where `webhook_id.txt` is stored), so the next start generates a fresh webhook id and overwrites `/config/.mcp_proxy_config.json` with it. Update your MCP client (and re-add the Claude.ai connector) with the new URL afterwards.
- **Uninstalling** the app does not automatically remove the custom integration files. To fully clean up after uninstalling:
  1. Delete `/config/custom_components/mcp_proxy/`
  2. Delete `/config/.mcp_proxy_config.json`
  3. Delete `/config/.mcp_proxy_inbound.log` (only present if you used **Log inbound requests**; normally removed when the app stops)
  4. Restart Home Assistant

## Support

**Issues:** https://github.com/homeassistant-ai/ha-mcp/issues
**Documentation:** https://github.com/homeassistant-ai/ha-mcp
