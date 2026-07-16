# Run the MCP server inside Home Assistant

The **HA-MCP Custom Component** (`ha_mcp_tools`) can run the **full ha-mcp server
in-process**, inside the Home Assistant application, and expose it remotely
through a Home Assistant webhook. This is one of the ways to run ha-mcp — and the
recommended one.

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
  add-on. It works on HAOS too — the add-on remains a supported alternative, but
  you only need one of them; the two are fully independent.

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

## Requirements

The in-process server requires **Home Assistant 2026.6.0 or newer**. Older Core
releases constrain dependencies to versions that cannot run current `ha-mcp`
servers. On older releases, the component remains available for its HA MCP Tools
entry with an external add-on or Docker server, but its config flow blocks
creation of the incompatible in-process server entry.

## Setup

1. **Install the component.** Install **HA-MCP Custom Component** from HACS
   (repository `homeassistant-ai/ha-mcp-integration`, the component's HACS
   distribution mirror), or, without
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
   **HA-MCP Server** confirms it is running and points you to the URL. The
   connect URL itself is on the entry's **Configure** screen (**Settings →
   Devices & Services → HA-MCP Custom Component → HA-MCP Server → Configure**)
   and in the Home Assistant log — both admin-only surfaces, because the URL is
   the credential. The notification deliberately carries no URL: notifications
   are visible to every signed-in user.
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

The server is also reachable directly on its own port (default - same model
as the add-on), bypassing the webhook, at the secret path (which looks like
`/private_<random>`):

- **Direct LAN access:** `http://<home-assistant-ip>:9584/private_<random>`

Set **Network access** to `127.0.0.1` to turn direct access off and keep only
the webhook and panel paths. All connect URLs — the webhook forms and, whenever
direct access is on, the direct URL — are listed on the entry's Configure
screen and in the Home Assistant log.

## Chat with the toolset from Home Assistant (conversation agents / voice)

While the server is running, its toolset is also registered as a Home
Assistant **LLM API** named after the entry. Any conversation agent — OpenAI,
Google Generative AI, Anthropic, Ollama, or any other integration that
supports LLM APIs — can select it, with the LLM of your choice, cloud or
local:

1. Add a conversation-agent integration (for a fully local setup: **Ollama**).
2. In that agent's settings, under **Control Home Assistant**, select
   **HA-MCP Server (tool search)** (alongside or instead of the built-in
   Assist API).
3. Talk to the agent from the **Assist chat** dialog, the companion apps, or a
   **voice satellite** whose pipeline uses that agent — "create an automation
   that turns off the lights when everyone leaves" now runs through the
   ha-mcp tools.

### Exposure modes

The **Conversation-agent tool exposure** option picks the shape agents get:

- **Tool search** (default): a compact API — the pinned tools directly, plus
  `ha_search_tools` (find tools for a task) and `ha_call_tool` (run one).
  Keeps the agent's context small; works with modest local models.
- **Full catalog**: every exposed tool listed directly with its schema.
  Better tool selection for large-context models, at ~10× the prompt cost.
- **Both**: registers the two APIs side by side — each agent picks its own
  in the selector. One server serves both; nothing runs twice.

### Per-tool exposure

Which tools agents may see is managed per tool from the **HA-MCP settings
panel** (the new **LLM API** toggle next to enabled/pinned/security-gated).
It is deny-by-default for **beta** tools, **developer-mode** tools, and the
**restart / reload / backup** family — a hidden tool is simply invisible to
agents (absent from the catalog and from search results) while staying fully
available to your regular MCP clients. Changes apply on the agent's next
message, no restart. A tool disabled globally is off for everything,
everywhere; the security gate and Read Only Mode apply to agent calls exactly
as to any MCP client.

Notes:

- No MCP client, external URL, or token is involved: the agent reaches the
  server over loopback inside Home Assistant.
- Home Assistant conversation agents cap tool iterations per turn (around
  ten), so a very complex build may need a follow-up prompt to continue.

**Security:** the toolset runs with the server's admin access. Selecting it
on an agent hands that power to everyone who can talk to that agent,
including anyone within earshot of a voice satellite using it. Keep it off
pipelines where that is not intended.

To remove the API from every agent's selector entirely, turn off
**Conversation-agent LLM API** in the entry's [options](#options).

## Settings panel ("HA-MCP" in the sidebar)

While a server entry is running, the integration adds an **HA-MCP** panel
to the Home Assistant sidebar. It opens the server web settings UI (tool
enable/disable/pin, feature flags, backups, themes) without needing the
loopback URL - the same experience as the add-on "Open Web UI" button.

The panel is admin-only. Opening it establishes a short-lived session for
your Home Assistant login, and every request re-checks that the account is
still an active administrator. No token or secret ever appears in a URL,
and the secret path stays on the loopback side of the proxy.

## Independent from the add-on

The in-process server and the Home Assistant MCP Server add-on are completely
independent: neither requires the other, and there is nothing to configure
between them. The in-process server defaults to port **9584** while the add-on
uses **9583**, so an existing add-on install does not conflict.

## Options

Open **Settings → Devices & Services → HA-MCP Custom Component → HA-MCP Server → Configure** to change these. Saving the options reloads the server so
the changes take effect. (The **HA MCP Tools** services entry has no options — a
Configure there just reports that.)

| Option | Default | What it does |
|--------|---------|--------------|
| **Release channel** | `stable` | `stable` installs the latest stable release; `dev` installs the latest development build. Both channels update automatically (a reload or restart, plus a periodic check, install the newest build of the selected channel). See [Release channels](#release-channels). |
| **Automatic server updates** | on | When on, the selected channel's newest release is installed automatically (on reload/restart and via a periodic check). When off, the server stays on the version currently installed — new releases are still offered on the server's update entity, and its **Install** button installs one without turning automatic updates back on. Governs the ha-mcp **server package** only — component updates still come through HACS. A package override below overrides this. |
| **Server port** | `9584` | Local TCP port the server listens on. `9584` avoids the add-on's `9583` so an existing add-on install does not conflict. |
| **Network access** | `0.0.0.0` | The default matches the add-on: the port is reachable on your LAN with the secret path as the credential. `127.0.0.1` restricts direct access to the Home Assistant machine (the webhook and panel work either way). |
| **Authentication mode** | `none` | `none`: the secret webhook URL is the credential. `ha_auth`: clients sign in with your Home Assistant account. `legacy`: self-hosted OAuth with a static Client ID + Secret, for OAuth-only clients (Gemini Spark, Copilot CLI). See [Security](#security). |
| **ha-mcp package (advanced)** | empty (tracks the selected release channel) | The pip requirement installed at runtime. Leave it empty unless you are testing a pre-release — it accepts any pip requirement string, including a version pin or a GitHub tarball URL. An explicit value overrides the release channel and **disables automatic updates** (a pin stays put until you clear it); changing it forces a reinstall on the next reload. |
| **Home Assistant URL for the server (advanced)** | `http://127.0.0.1:8123` | How the in-process server reaches Home Assistant. The loopback default works for almost everyone; only change it for unusual SSL-only setups. |
| **Remote access via webhook** | on | Turn off for local-only mode: the webhook is never registered, so Home Assistant (including Nabu Casa) cannot reach the server at all. Direct port access and the sidebar panel keep working. |
| **Conversation-agent LLM API** | on | Offers the toolset to Home Assistant conversation agents — see [Chat with the toolset](#chat-with-the-toolset-from-home-assistant-conversation-agents--voice). Enabling only makes it selectable per agent; turn off to remove it from every agent's selector. |
| **Conversation-agent tool exposure** | `tool_search` | Shape of the toolset agents get: compact tool-search API (default), the full catalog, or both side by side (choose per agent). See [Exposure modes](#exposure-modes). |
| **External URL (optional)** | empty | Shown as the primary connect URL - for your own domain / reverse proxy (e.g. `https://ha.example.com`). Opening it should reach your HA login page, and must not contain a port like `:8123` (any port breaks remote MCP clients). Empty = Nabu Casa / local automatically. |
| **Custom webhook secret (optional)** | empty | Replaces the random webhook secret in `/api/webhook/<secret>`. The URL is the credential - use a long, hard-to-guess value. |
| **Custom direct-access path (optional)** | empty | Replaces the random `/private_...` path on the server port. Same rule: the path is the credential. |
| **Regenerate connect secrets now** | off | One-time action: mints fresh random values for both secrets, immediately invalidating the old connect URLs (and clearing the two overrides). |

### Release channels

The **Release channel** option selects which build of the server is installed.
Both channels install unpinned and **update automatically**:

- **`stable` (default):** the latest `ha-mcp` release from PyPI.
- **`dev`:** the latest development build, published to PyPI as `ha-mcp-dev` on
  every change to the project's main branch. Use it to try upcoming fixes, and
  expect the occasional rough edge.

While **Automatic server updates** is on (the default), both channels install
unpinned: an entry reload or a Home Assistant restart always reinstalls the
newest build of the selected channel, and on top of that the component checks
PyPI for a newer build every 6 hours and reloads the entry automatically when
one is published — so a long-running instance picks up releases without a
restart. The reload applies the new server code immediately (component >=
1.0.1 reloads the module cache per worker start); only updates that require
newer *third-party dependencies* still need a Home Assistant core restart.
The web settings UI's **Restart HA-MCP Server** button performs the same
entry reload. Each automatic update also raises a notification naming the old and
new version, with a link to the release notes. Turn **Automatic server
updates** off to freeze the server on the version currently installed:
reloads/restarts keep that exact version until you turn it back on or install
a newer build yourself from the update entity (this governs the server package
only — component updates still arrive through HACS). Setting the **ha-mcp
package (advanced)** field overrides the channel entirely (pin a version, or
install from a URL for pre-release testing) and also disables automatic updates
until you clear it.

The server's version is always visible on its **update entity**, under
**Settings → Devices & Services → HA-MCP Custom Component → HA-MCP Server**
(and under **Settings → System → Updates** whenever an update is available).
The entity shows the installed and latest version of the selected channel and
links the release notes — the 6-hour PyPI check keeps it populated even with
automatic updates off, where its **Install** button installs the offered
version on your schedule. The server (`7.x`) and the component (`1.x`) are
versioned independently: this entity and HACS each own one of the two numbers.

Switching channels reinstalls the server from the other channel on the next
reload. `ha-mcp` and `ha-mcp-dev` share the same import package, so the previous
channel's package is uninstalled first — only one is ever installed at a time.

If the installed server needs a newer version of the custom component than the
one you have (HACS can deliver a server build before you update the component),
a repair issue titled **Update the HA-MCP Custom Component via HACS** appears
under **Settings → Repairs** with a link to the HACS update. The server keeps
running; update the component via HACS to clear it.

If the component was installed from the legacy location — the main `ha-mcp`
server repository added directly as a HACS custom repository, before the
dedicated [`ha-mcp-integration`](https://github.com/homeassistant-ai/ha-mcp-integration)
mirror existed — a repair issue titled **Component installed from the legacy
repository** appears. Such an install keeps working, but HACS displays the
server's `7.x` version numbers and the server's release notes for the
component. Follow the issue's link to add the mirror in HACS and reinstall the
component from it (your settings and config entries are kept), then restart
Home Assistant; the issue clears itself afterwards.

### Local-only mode

Turn **Remote access via webhook** off to keep the server unreachable through
Home Assistant entirely - no webhook means no Nabu Casa path and no
`/api/webhook/...` endpoint. You keep direct access on the server port (with
the secret path) and the admin-only sidebar panel.

### Rotating your connect URL

If a connect URL may have leaked, open the entry's options and check
**Regenerate connect secrets now**, then save - both the webhook secret and the
direct-access path are re-minted on the spot and every old URL stops working.
Update your MCP clients with the new URL from the Configure screen. (Removing and
re-adding the entry also rotates everything, including the internal token.)

## Security

The in-process server offers three authentication postures, chosen with the
**Authentication mode** option:

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
- **`legacy`: a self-hosted OAuth server with a static Client ID + Secret.** For
  OAuth-only clients that `ha_auth` can't serve (Google Gemini Spark, GitHub
  Copilot CLI). The component runs its own OAuth 2.1 authorization server at the
  Home Assistant root and issues a static **Client ID / Client Secret** to paste
  into the client — the secret *is* the credential and grants admin-equivalent
  access, so guard it like the `none` URL. Tokens are self-issued (1h access /
  30d refresh) and carry no Home Assistant login; **rotating** the credential
  (regenerate toggle, or a custom Client ID / Secret override) invalidates
  outstanding tokens, but only after the Home Assistant restart the repair
  prompts for. See [SECURITY.md](../SECURITY.md#in-process-server-ha_mcp_tools-in-process-server-entry)
  for the full threat model (unauthenticated consent page, permissive redirect
  URIs, route ownership vs the add-on).

All three postures ride Home Assistant's own remote access (Nabu Casa / your
reverse proxy) for TLS. If you expose the server to the internet, prefer
`ha_auth`; keep the `none` URL — or a `legacy` Client Secret — strictly private.

The server reaches Home Assistant with a dedicated admin token the component
provisions and stores in the config entry; that token is handed to the server
in-memory (never through the Home Assistant process environment). Removing the
entry revokes it. As with every deployment, that token's Home Assistant
permissions define what the server can do.

See [SECURITY.md](../SECURITY.md) for the full threat model.

## Relationship to the tools services entry

The in-process server entry and the **HA MCP Tools** services entry are two
config-entry types of the same **HA-MCP Custom Component** (`ha_mcp_tools`). They
are independent: the server works on its own, and most installs never need the
tools entry. Add it only if you enable ha-mcp's opt-in file and YAML editing
tools (feature flags, off by default) — those tools call the privileged services
the tools entry registers, and that applies to every server type, including the
in-process server. Add or remove it at any time from the same **Add Integration**
menu (choose **HA MCP Tools**); it changes nothing about how the in-process
server runs.

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

**The Configure screen shows only a webhook path.** If Home Assistant cannot
determine an external or internal URL, the Configure screen and log show the
webhook path on its own (`/api/webhook/<webhook-id>`); prefix it with your Home
Assistant URL. Set your internal/external URLs under **Settings → System →
Network** so the full URL is shown.
