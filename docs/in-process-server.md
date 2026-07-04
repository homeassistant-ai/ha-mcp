# Run the MCP server inside Home Assistant

The **Home Assistant MCP Server** integration (`ha_mcp_server`) runs the **full
ha-mcp server in-process**, inside the Home Assistant application, and exposes it
remotely through a Home Assistant webhook. This is a fourth way to run ha-mcp,
alongside the add-on, Docker, and the local stdio setup.

It is a standalone integration, separate from the `ha_mcp_tools` custom
component. You can run either on its own, but they are complementary — see
[Relationship to `ha_mcp_tools`](#relationship-to-ha_mcp_tools) below.

## Who it's for

- **Home Assistant Container and Home Assistant Core users**, who cannot install
  add-ons (add-ons require the Supervisor). Instead of running ha-mcp in a
  separate Docker container or over stdio, you run it inside Home Assistant
  itself.
- **Home Assistant OS / Supervised users** who would rather not run a separate
  add-on. It works on HAOS too — the add-on is still the recommended path there,
  but the in-process server is a supported alternative, and the two can run side
  by side (they default to different ports).

Because it reaches the internet through a Home Assistant webhook, the connect URL
works through **Nabu Casa remote UI** (or any reverse proxy pointing at Home
Assistant) with no separate tunnel or port forwarding — the same mechanism the
Webhook Proxy add-on uses.

## How it works

Once the integration's config entry exists, it:

1. Installs the `ha-mcp` package into Home Assistant at runtime (the first start
   takes a little longer while pip downloads it — see
   [First start](#first-start-takes-a-little-longer) below).
2. Provisions a dedicated Home Assistant admin token the server uses to reach
   Home Assistant over loopback.
3. Runs the server on its own thread so a slow tool call can never stall Home
   Assistant's event loop.
4. Registers a Home Assistant webhook that forwards MCP traffic to the server, so
   it is reachable remotely with the webhook URL as the secret.

The bring-up runs in the background, so it never delays Home Assistant startup.

## Setup

1. **Install the integration.** A HACS listing is planned but not yet available,
   so for now copy the `homeassistant-integration/ha_mcp_server` directory from
   this repository into your Home Assistant `config/custom_components/` directory
   (so you end up with `config/custom_components/ha_mcp_server/`), then restart
   Home Assistant.
2. **Add the integration.** Go to **Settings → Devices & Services → Add
   Integration**, search for **Home Assistant MCP Server**, and submit the
   confirmation. Creating the entry starts the server with the defaults.
3. **Copy your connect URL.** As soon as the server starts, a notification titled
   **Home Assistant MCP Server** appears under **Settings → Notifications** with
   the connect URL(s). The same URL is shown on the integration's **Configure**
   screen and written to the Home Assistant log.
4. **Connect your MCP client** to that URL.

To pause the server, **disable** the config entry (**Settings → Devices &
Services → Home Assistant MCP Server → ⋮ → Disable**); re-enable it to start it
again. Removing the integration stops the server and revokes the provisioned
token.

## Connect URLs

The server is reached through a Home Assistant webhook whose id is your secret
(it looks like `mcp_` followed by a long random string):

- **Remote (Nabu Casa or any external URL):**
  `https://<your-nabu-casa-domain>/api/webhook/<webhook-id>`
- **Local network:**
  `http://<home-assistant-host>:8123/api/webhook/<webhook-id>`

If you set **Bind address** to `0.0.0.0` (see [Options](#options)), the server is
also reachable directly on its own port, bypassing the webhook, at the secret
path (which looks like `/private_<random>`):

- **Direct LAN access:** `http://<home-assistant-ip>:9584/private_<random>`

The remote and local webhook URLs are listed in the notification and on the
Configure screen; the direct URL is listed too whenever the bind address is
`0.0.0.0`.

## Coexisting with the add-on

The in-process server defaults to port **9584**, while the Home Assistant MCP
Server add-on uses **9583**. You can run both at once — for example, keep the
add-on for local clients and use the in-process server's webhook URL for remote
access — without a port conflict, as long as you leave the default port (or pick
another free one).

## Options

Open **Settings → Devices & Services → Home Assistant MCP Server → Configure** to
change these. Saving the options reloads the server so the changes take effect.

| Option | Default | What it does |
|--------|---------|--------------|
| **Server port** | `9584` | Local TCP port the server listens on. `9584` avoids the add-on's `9583` so both can run at once. |
| **Bind address** | `127.0.0.1` | `127.0.0.1` keeps the server loopback-only (remote access is via the webhook). `0.0.0.0` additionally allows direct access from your LAN at the secret path. |
| **Webhook authentication** | `none` | `none`: the secret webhook URL is the credential. `ha_auth`: clients sign in with your Home Assistant account. See [Security](#security). |
| **ha-mcp package (advanced)** | `ha-mcp==7.9.0` | The pip requirement installed at runtime. Leave it unless you are testing a pre-release — it accepts any pip requirement string, including a GitHub tarball URL. Changing it forces a reinstall on the next reload. |
| **Home Assistant URL for the server (advanced)** | `http://127.0.0.1:8123` | How the in-process server reaches Home Assistant. The loopback default works for almost everyone; only change it for unusual SSL-only setups. |

## Security

The in-process server offers two authentication postures, chosen with the
**Webhook authentication** option:

- **`none` (default): the secret webhook URL is the credential.** The webhook id
  is a high-entropy random string, and anyone who has the full URL can reach the
  server — exactly like the Webhook Proxy add-on's default. When exposed through
  Nabu Casa (or another HTTPS reverse proxy) the URL travels over TLS. Treat the
  URL like a password: don't share it or paste it where it could be logged.
- **`ha_auth`: clients sign in with your Home Assistant account.** Home Assistant
  Core acts as the OAuth authorization server. MCP clients that support OAuth
  (for example claude.ai and ChatGPT) discover the sign-in endpoints
  automatically and authenticate the user against Home Assistant; requests
  without a valid Home Assistant token are rejected. There is no separate
  password or credential to manage — it is your existing Home Assistant login.

Both postures ride Home Assistant's own remote access (Nabu Casa / your reverse
proxy) for TLS. If you expose the server to the internet, prefer `ha_auth`, or
keep the `none` URL strictly private.

The server reaches Home Assistant with a dedicated admin token the integration
provisions and stores in the config entry; that token is handed to the server
in-memory (never through the Home Assistant process environment). Removing the
integration revokes it. As with every deployment, that token's Home Assistant
permissions define what the server can do.

See [SECURITY.md](../SECURITY.md) for the full threat model.

## Relationship to `ha_mcp_tools`

`ha_mcp_server` (this integration) and `ha_mcp_tools` (the file/YAML services
custom component) are independent. The server works on its own, but installing
`ha_mcp_tools` alongside it is recommended: it provides the privileged file and
YAML-configuration services that ha-mcp's file tools use — exactly as it does for
the add-on, Docker, and pip deployments. Install `ha_mcp_tools` as described in
the [README](../README.md#-custom-component-ha_mcp_tools-beta); it is optional and
changes nothing about how the in-process server runs.

## First start takes a little longer

The first time the server starts, the integration downloads and installs the
`ha-mcp` package with pip. This can take a minute or two — occasionally longer —
depending on your connection and hardware; the server starts automatically once
the install finishes. Later restarts are fast because the package is already
installed.

## Troubleshooting

**The server won't start.** If the server fails to come up — for example because
the port is already in use, or token provisioning fails — a repair issue titled
**The Home Assistant MCP Server failed to start** appears under **Settings →
Repairs**, carrying the specific reason. If the `ha-mcp` package itself can't be
installed, the repair issue is titled **The Home Assistant MCP Server package
could not be installed** instead. Fix the cause — check the Home Assistant log and
your network connectivity for an install failure, or set a different **Server
port** for a port conflict — then reload the integration (save the options, or use
**⋮ → Reload**) to retry.

**Nothing happens after updating the integration.** Home Assistant loads custom
integration code at startup, so after you copy in a new version you must
**restart Home Assistant** for the update to take effect.

**Where the logs are.** The in-process server logs into the normal Home Assistant
log (**Settings → System → Logs**, or `home-assistant.log`). Its working data
lives in `.ha_mcp_server/` under your Home Assistant config directory.

**The connect URL isn't in the notification.** If Home Assistant cannot determine
an external or internal URL, the notification and Configure screen show the
webhook path on its own (`/api/webhook/<webhook-id>`); prefix it with your Home
Assistant URL. Set your internal/external URLs under **Settings → System →
Network** so the full URL is shown.
