# Run the MCP server inside Home Assistant

The **HA-MCP Custom Component** (`ha_mcp_tools`) can run the **full ha-mcp server
in-process**, inside the Home Assistant application, and expose it remotely
through a Home Assistant webhook. This is a fourth way to run ha-mcp, alongside
the add-on, Docker, and the local stdio setup.

The in-process server is one of **two config-entry types** the component offers.
The other is the **HA MCP Tools** services entry (the privileged file / YAML
services). They are complementary and independent — you can add either, or both,
under the one integration — see [Relationship to the tools services
entry](#relationship-to-the-tools-services-entry) below.

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

Once the in-process server config entry exists, it:

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

1. **Install the component.** Install **HA-MCP Custom Component** from HACS (the
   same repository you use for ha-mcp — no second repository to add), or, without
   HACS, copy the `custom_components/ha_mcp_tools` directory from this repository
   into your Home Assistant `config/custom_components/` directory (so you end up
   with `config/custom_components/ha_mcp_tools/`). Restart Home Assistant.
2. **Add the in-process server entry.** Go to **Settings → Devices & Services →
   Add Integration**, search for **HA-MCP Custom Component**, and — on the menu
   that appears — choose **HA-MCP Server**, then submit the confirmation.
   Creating the entry starts the server with the defaults. (If you already have
   the **HA MCP Tools** services entry, use the same **Add Integration** flow;
   the two entries appear together under the one integration tile.)
3. **Copy your connect URL.** As soon as the server starts, a notification titled
   **HA-MCP in-process server** appears under **Settings → Notifications** with
   the connect URL(s). The same URL is shown on the entry's **Configure** screen
   and written to the Home Assistant log.
4. **Connect your MCP client** to that URL.

To pause the server, **disable** its config entry (**Settings → Devices &
Services → HA-MCP Custom Component → HA-MCP Server → ⋮ → Disable**);
re-enable it to start it again. Removing the entry stops the server and revokes
the provisioned token.

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

## Settings panel ("HA-MCP" in the sidebar)

While a server entry is running, the integration adds an **HA-MCP** panel
to the Home Assistant sidebar. It opens the server web settings UI (tool
enable/disable/pin, feature flags, backups, themes) without needing the
loopback URL - the same experience as the add-on "Open Web UI" button.

The panel is admin-only. Opening it establishes a short-lived session for
your Home Assistant login, and every request re-checks that the account is
still an active administrator. No token or secret ever appears in a URL,
and the secret path stays on the loopback side of the proxy.

## Coexisting with the add-on

The in-process server defaults to port **9584**, while the Home Assistant MCP
Server add-on uses **9583**. You can run both at once — for example, keep the
add-on for local clients and use the in-process server's webhook URL for remote
access — without a port conflict, as long as you leave the default port (or pick
another free one).

## Options

Open **Settings → Devices & Services → HA-MCP Custom Component → HA-MCP Server → Configure** to change these. Saving the options reloads the server so
the changes take effect. (The **HA MCP Tools** services entry has no options — a
Configure there just reports that.)

| Option | Default | What it does |
|--------|---------|--------------|
| **Release channel** | `stable` | `stable` installs the pinned, tested release; `dev` installs the latest development build, refreshed on every reload or restart. See [Release channels](#release-channels). |
| **Server port** | `9584` | Local TCP port the server listens on. `9584` avoids the add-on's `9583` so both can run at once. |
| **Bind address** | `127.0.0.1` | `127.0.0.1` keeps the server loopback-only (remote access is via the webhook). `0.0.0.0` additionally allows direct access from your LAN at the secret path. |
| **Webhook authentication** | `none` | `none`: the secret webhook URL is the credential. `ha_auth`: clients sign in with your Home Assistant account. See [Security](#security). |
| **ha-mcp package (advanced)** | `ha-mcp==7.9.0` | The pip requirement installed at runtime. Leave it unless you are testing a pre-release — it accepts any pip requirement string, including a GitHub tarball URL. An explicit value overrides the release channel, and changing it forces a reinstall on the next reload. |
| **Home Assistant URL for the server (advanced)** | `http://127.0.0.1:8123` | How the in-process server reaches Home Assistant. The loopback default works for almost everyone; only change it for unusual SSL-only setups. |

### Release channels

The **Release channel** option selects which build of the server is installed:

- **`stable` (default):** the pinned `ha-mcp` release. Its version is kept in
  lockstep with the project's releases by the release pipeline, so it only
  changes when you update the component (and restart Home Assistant).
- **`dev`:** the latest development build, published to PyPI as `ha-mcp-dev` on
  every change to the project's main branch. Because it moves quickly, the
  server reinstalls the newest dev build on every entry reload and Home Assistant
  restart — so a restart always lands on the current dev build. Use it to try
  upcoming fixes, and expect the occasional rough edge.

Switching channels reinstalls the server from the other channel on the next
reload. `ha-mcp` and `ha-mcp-dev` share the same import package, so the previous
channel's package is uninstalled first — only one is ever installed at a time.
The **ha-mcp package (advanced)** field overrides the channel entirely: set it to
pin a specific version or install from a URL for pre-release testing.

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
  without a valid Home Assistant token are rejected. Only **administrator**
  accounts are accepted: the server performs its Home Assistant operations with
  its own provisioned admin token, so a non-admin login is refused rather than
  silently granted admin-equivalent control. There is no separate password or
  credential to manage — it is your existing Home Assistant admin login.

Both postures ride Home Assistant's own remote access (Nabu Casa / your reverse
proxy) for TLS. If you expose the server to the internet, prefer `ha_auth`, or
keep the `none` URL strictly private.

The server reaches Home Assistant with a dedicated admin token the component
provisions and stores in the config entry; that token is handed to the server
in-memory (never through the Home Assistant process environment). Removing the
entry revokes it. As with every deployment, that token's Home Assistant
permissions define what the server can do.

See [SECURITY.md](../SECURITY.md) for the full threat model.

## Relationship to the tools services entry

The in-process server entry and the **HA MCP Tools** services entry are two
config-entry types of the same **HA-MCP Custom Component** (`ha_mcp_tools`). They
are independent: the server works on its own, but adding the tools services entry
alongside it is recommended — it provides the privileged file and
YAML-configuration services that ha-mcp's file tools use, exactly as it does for
the add-on, Docker, and pip deployments. Add it from the same **Add Integration**
menu (choose **HA MCP Tools**); it is optional and changes nothing about how the
in-process server runs.

## First start takes a little longer

The first time the server starts, the component downloads and installs the
`ha-mcp` package with pip. This can take a minute or two — occasionally longer —
depending on your connection and hardware; the server starts automatically once
the install finishes. Later restarts are fast because the package is already
installed.

## Troubleshooting

**The server won't start.** If the server fails to come up — for example because
the port is already in use, or token provisioning fails — a repair issue titled
**The HA-MCP in-process server failed to start** appears under **Settings →
Repairs**, carrying the specific reason. If the `ha-mcp` package itself can't be
installed, the repair issue is titled **The HA-MCP in-process server package
could not be installed** instead. Fix the cause — check the Home Assistant log and
your network connectivity for an install failure, or set a different **Server
port** for a port conflict — then reload the entry (save the options, or use
**⋮ → Reload**) to retry.

**Nothing happens after updating the component.** Home Assistant loads custom
integration code at startup, so after HACS (or a manual copy) delivers a new
version you must **restart Home Assistant** for the update to take effect.

**Skill guidance is empty after installing from a GitHub tarball.** The **ha-mcp
package (advanced)** field can install from a GitHub tarball URL, but a git
archive excludes submodules — and the bundled skill content ships as a submodule.
A tarball install therefore omits it, so the skill-guidance tools report empty
listings. Install from PyPI instead (either release channel includes the skill
content); the tarball override is only meant for quick pre-release testing.

**Where the logs are.** The in-process server logs into the normal Home Assistant
log (**Settings → System → Logs**, or `home-assistant.log`). Its working data
lives in `.ha_mcp/` under your Home Assistant config directory.

**The connect URL isn't in the notification.** If Home Assistant cannot determine
an external or internal URL, the notification and Configure screen show the
webhook path on its own (`/api/webhook/<webhook-id>`); prefix it with your Home
Assistant URL. Set your internal/external URLs under **Settings → System →
Network** so the full URL is shown.
