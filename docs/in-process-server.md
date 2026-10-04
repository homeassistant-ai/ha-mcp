# Run the MCP server inside Home Assistant

The **HA-MCP Custom Component** (`ha_mcp_tools`) can run the **full ha-mcp server
in-process**, inside the Home Assistant application, and expose it remotely
through a Home Assistant webhook. This is one of the ways to run ha-mcp — and the
recommended one. It is a complete, standalone ha-mcp install — a replacement for
the app (add-on), Docker, and uvx/PyPI (stdio) server, not an addition to them. Run
only one ha-mcp server; never run the in-process server alongside another
install.

The in-process server is one of **two config-entry types** the component offers.
The other is the **File & YAML services entry** (**HA-MCP File & YAML Tools**),
the privileged file / YAML services. They are complementary and independent — you
can add either, or both, under the one integration — see [Relationship to the
File & YAML services entry](#relationship-to-the-file--yaml-services-entry) below.

## Who it's for

- **Home Assistant Container and Home Assistant Core users**, who cannot install
  apps (apps require the Supervisor). Instead of running ha-mcp in a
  separate Docker container or over stdio, you run it inside Home Assistant
  itself.
- **Home Assistant OS / Supervised users** who would rather not run a separate
  app. It works on HAOS too — the app remains a supported alternative, but
  you only need one of them; the two are fully independent.

Because it reaches the internet through a Home Assistant webhook, the connect URL
works through **Nabu Casa remote UI** (or any reverse proxy pointing at Home
Assistant) with no separate tunnel or port forwarding — the same mechanism the
Webhook Proxy app uses.

## How it works

Once the in-process server config entry exists, it:

1. Runs the `ha-mcp` server build this component release pins. Home Assistant
   installs it as an integration requirement when it loads the component (the
   first start takes a little longer while it downloads — see
   [First start](#first-start-takes-a-little-longer) below).
2. Connects to Home Assistant over loopback with the administrator access token
   you enter during setup.
3. Runs the server on its own thread so a slow tool call can never stall Home
   Assistant's event loop.
4. Registers a Home Assistant webhook that forwards MCP traffic to the server, so
   it is reachable remotely with the webhook URL as the secret.

The bring-up runs in the background, so it never delays Home Assistant startup.

## Requirements

Three floors apply, from the outside in:

- **HACS installs the component only on Home Assistant 2026.8.0 or newer**
  (`hacs.json`), the floor the config flow also enforces for the in-process
  server entry.
- **The integration does not load at all before Core 2026.7.** From component
  2.1.3 the manifest declares `voluptuous-openapi`, and earlier Core releases
  pin that package to an older version, so the requirement cannot resolve and
  Home Assistant refuses to set up the integration, File & YAML entry included.
- **Between 2026.7 and 2026.8.0 a manual copy loads**, but the config flow
  blocks creation of the in-process server entry; the File & YAML services
  entry still works with an external app or Docker server.

Older Core releases also constrain other dependencies to versions that cannot
run current `ha-mcp` servers.

## Setup

1. **Install the component.** Install **HA-MCP Custom Component** from HACS
   (repository `homeassistant-ai/ha-mcp-integration`, the component's HACS
   distribution mirror), or, without
   HACS, copy the `custom_components/ha_mcp_tools` directory from this repository
   into your Home Assistant `config/custom_components/` directory (so you end up
   with `config/custom_components/ha_mcp_tools/`). Restart Home Assistant.
2. **Add the in-process server entry.** Go to **Settings → Devices & Services →
   Add Integration**, search for **HA-MCP Custom Component**, and — on the menu
   that appears — choose **HA-MCP Server**. The setup form asks for an
   **Administrator access token**: the server acts with that account's
   permissions, so it must be an administrator's long-lived access token. To
   create one, open your profile (your name at the bottom of the sidebar), select the **Security** tab, and under **Long-lived access tokens** select **Create token**. It also asks how MCP clients may reach
   the server: **Remote access through Home Assistant**
   (disabled by default; choose Home Assistant sign-in, legacy OAuth or the
   secret URL to let clients connect through a webhook) and **Network access**
   (this machine only by default; choose local network to let devices on your
   network use the direct port). Submitting creates the entry and starts the
   server. (If you already have
   the **HA-MCP File & YAML Tools** entry, use the same **Add Integration** flow;
   the two entries appear together under the one integration tile.)
3. **Copy your connect URL.** As soon as the server starts, a notification titled
   **HA-MCP Server** confirms it is running and points you to the URL. The
   connect URL itself is on the entry's **Configure** screen (**Settings →
   Devices & Services → HA-MCP Custom Component → HA-MCP Server → Configure**),
   which only administrators can open, because the URL is the credential. The
   notification and the Home Assistant log deliberately carry no URL:
   notifications are visible to every signed-in user, and the log reaches
   connected MCP clients through the server's own log tools.
4. **Connect your MCP client** to that URL.

To pause the server, **disable** its config entry (**Settings → Devices &
Services → HA-MCP Custom Component → HA-MCP Server → ⋮ → Disable**);
re-enable it to start it again. Removing the entry stops the server; the token
you entered stays valid until you delete it from your profile.

## Connect URLs

The server is reached through a Home Assistant webhook whose id is your secret
(it looks like `mcp_` followed by a long random string):

- **Remote (Nabu Casa or any external URL):**
  `https://<your-nabu-casa-domain>/api/webhook/<webhook-id>`
- **Local network:**
  `http://<home-assistant-host>:8123/api/webhook/<webhook-id>`

The server is also reachable directly on its own port (default - same model
as the app), bypassing the webhook, at the secret path (which looks like
`/private_<random>`):

- **Direct LAN access:** `http://<home-assistant-ip>:9584/private_<random>`

Set **Network access** to `127.0.0.1` to turn direct access off and keep only
the webhook and panel paths. All connect URLs — the webhook forms and, whenever
direct access is on, the direct URL — are listed on the entry's Configure
screen.

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
- On Home Assistant 2026.10+, each tool carries its title and its read-only /
  destructive / idempotent / open-world hints, so agents and approval
  prompts can tell a lookup from a change.
- With Home Assistant 2026.10+'s own Model Context Protocol server turned
  on, **Settings → System → AI** also lists this API's own URL
  (`/api/mcp/<api id>`). External MCP clients signed in as an administrator
  can use it, whichever APIs the server entry itself selects.

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
loopback URL - the same experience as the app "Open Web UI" button.

The panel is admin-only. Opening it establishes a short-lived session for
your Home Assistant login, and every request re-checks that the account is
still an active administrator. No token or secret ever appears in a URL,
and the secret path stays on the loopback side of the proxy.

## Independent from the app

The in-process server and the Home Assistant MCP Server app are completely
independent: neither requires the other, and there is nothing to configure
between them. The in-process server defaults to port **9584** while the app
uses **9583**, so an existing app install does not conflict.

## Options

Open **Settings → Devices & Services → HA-MCP Custom Component → HA-MCP Server → Configure** to change these. Saving the options reloads the server so
the changes take effect; a change to the OAuth callback list alone applies to the
next sign-in without a reload. (The **HA-MCP File & YAML Tools** entry has no options — a
Configure there just reports that.)

| Option | Default | What it does |
|--------|---------|--------------|
| **Server port** | `9584` | Local TCP port the server listens on. `9584` avoids the app's `9583` so an existing app install does not conflict. |
| **Network access** | this machine only (`127.0.0.1`) for new installs | `127.0.0.1` restricts direct access to the Home Assistant machine. Local network (`0.0.0.0`) makes the port reachable on your LAN with the secret path as the credential, like the app. The webhook and panel work either way. Entries created before setup asked for it keep local network unless you change it. |
| **Authentication mode** | as chosen at setup (`ha_auth` when remote access was left disabled) | `none`: the secret webhook URL is the credential. `ha_auth`: clients sign in with your Home Assistant account. `legacy`: self-hosted OAuth with a static Client ID + Secret, for clients that need a credential to paste. See [Security](#security). |
| **ha-mcp package (advanced)** | empty (the server this component release installed) | Leave it empty unless you are testing a specific build — it accepts any pip requirement string, including a version pin, a pull-request tarball or a wheel URL. Saving it reinstalls the server and restarts it in place, without restarting Home Assistant; clearing it returns to the paired server. See [Server updates](#server-updates). |
| **Home Assistant URL for the server (advanced)** | empty (derived from your HA's http config) | How the in-process server reaches Home Assistant. Empty derives the loopback URL from your instance's real port and SSL setting (an SSL-enabled HA is reached over `https://127.0.0.1` with certificate verification off — the certificate never matches a loopback address). Only set a value when the server must take a different route entirely. |
| **Remote access via webhook** | off for new installs (as chosen at setup) | Turn off for local-only mode: the webhook is never registered, so Home Assistant (including Nabu Casa) cannot reach the server at all. Direct port access and the sidebar panel keep working. |
| **Conversation-agent LLM API** | on | Offers the toolset to Home Assistant conversation agents — see [Chat with the toolset](#chat-with-the-toolset-from-home-assistant-conversation-agents--voice). Enabling only makes it selectable per agent; turn off to remove it from every agent's selector. |
| **Conversation-agent tool exposure** | `tool_search` | Shape of the toolset agents get: compact tool-search API (default), the full catalog, or both side by side (choose per agent). See [Exposure modes](#exposure-modes). |
| **External URL (optional)** | empty | Shown as the primary connect URL - for your own domain / reverse proxy (e.g. `https://ha.example.com`). Opening it should reach your HA login page, and must not contain a port like `:8123` (any port breaks remote MCP clients). Empty = Nabu Casa / local automatically. |
| **Custom webhook secret (optional)** | empty | Replaces the random webhook secret in `/api/webhook/<secret>`. The URL is the credential - use a long, hard-to-guess value. |
| **Custom direct-access path (optional)** | empty | Replaces the random `/private_...` path on the server port. Same rule: the path is the credential. |
| **Regenerate connect secrets now** | off | One-time action: mints fresh random values for both secrets, immediately invalidating the old connect URLs (and clearing the two overrides). |
| **Allowed OAuth callback URL** | claude.ai's callback | In `none` mode, the only callback URLs an OAuth sign-in is sent back to. Also editable in the **HA-MCP** sidebar panel under **Server Settings → Remote access**. See [Security](#security). |

### Server updates

The server is part of the component release. Each HA-MCP Custom Component
release names the exact `ha-mcp` build it was released with as an integration
requirement, and Home Assistant installs it like any integration's Python
packages. HACS is therefore the only update path: when it offers a component
update, that update carries the matching server, and restarting Home Assistant
after the update installs both. The component never downloads or swaps the
server on its own.

**Development builds** follow the main branch. Turn on the HACS **Pre-release**
switch for the HA-MCP Custom Component repository to receive them; each
pre-release pins the exact `ha-mcp-dev` build published from the same commit.
Turn the switch off to return to stable releases with the next stable update.
Moving between the two swaps `ha-mcp-dev` and `ha-mcp` as described below.

**Testing a specific build** is what the **ha-mcp package (advanced)** option is
for. Set it to a pip requirement — a version pin, a pull-request tarball such
as `https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/<PR>/head.tar.gz`,
or a wheel URL — and save: the server entry reloads, installs that build and
restarts the server in place, with no Home Assistant restart. Clear the field to
return to the server this component release installed. Home Assistant puts the
paired server back each time it starts, so with an override set, every Home
Assistant restart installs the override again afterwards (this needs network
access at startup).

`ha-mcp` and `ha-mcp-dev` share the same import package. When the pin or an
override installs one of them while the other is present, the other is
uninstalled and the requested one reinstalled, so only one is ever installed at
a time.

If a server installed through the override needs a newer version of the custom
component than the one you have, a repair issue titled **Update the HA-MCP Custom
Component via HACS** appears under **Settings → Repairs**. The server keeps
running; update the component via HACS to clear it.

Upgrading from component 2.x: the **Release channel** and **Automatic server
updates** options and the server update entity are gone (the entity is removed
from the registry on the first start). A 2.x install on the `dev` channel moves
to the stable server with this release; turn on the HACS **Pre-release** switch
to keep receiving development builds.

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

- **`none`: the secret webhook URL is the credential.** The webhook id
  is a high-entropy random string, and anyone who has the full URL can reach the
  server — exactly like the Webhook Proxy app's default. When exposed through
  Nabu Casa (or another HTTPS reverse proxy) the URL travels over TLS. Treat the
  URL like a password: don't share it or paste it where it could be logged.
  Entries created before setup asked for an authentication mode run in this mode.

  Some connectors (claude.ai among them) insist on an OAuth sign-in even here.
  The server approves it without a login, because the token it issues grants
  nothing, but it sends the sign-in back only to a callback URL on the
  **Allowed OAuth callback URL** list. The list starts with claude.ai's callback;
  for any other OAuth client, add the callback URL its sign-in uses, exactly as
  sent. An `http://` loopback callback (`127.0.0.1`, `[::1]` or `localhost`) also
  matches on any port, because desktop clients pick a free port per sign-in. A
  sign-in to an unlisted callback stops on a page that names the URL and where
  to add it; registering a client cannot add to the list. Clients that connect
  with the webhook URL alone need no entry. Before this list existed, any
  callback was accepted, so a non-claude.ai OAuth client that worked before
  needs its callback added after upgrading.
- **`ha_auth`: clients sign in with your Home Assistant account.** Home Assistant
  Core acts as the OAuth authorization server. MCP clients that support OAuth
  (for example claude.ai and ChatGPT) discover the sign-in endpoints
  automatically and authenticate the user against Home Assistant; requests
  without a valid Home Assistant token are rejected. Only **administrator**
  accounts are accepted: the server performs its Home Assistant operations with
  its own administrator token, so a non-admin login is refused rather than
  silently granted admin-equivalent control. There is no separate password or
  credential to manage — it is your existing Home Assistant admin login.
- **`legacy`: a self-hosted OAuth server with a static Client ID + Secret.** For
  clients that need a credential to paste (as of 2.0.0, `ha_auth` also serves
  Google Gemini Spark and GitHub Copilot CLI, so this is a fallback). The
  component runs its own OAuth 2.1 authorization server at the
  Home Assistant root and issues a static **Client ID / Client Secret** to paste
  into the client — the secret *is* the credential and grants admin-equivalent
  access, so guard it like the `none` URL. Tokens are self-issued (1h access /
  30d refresh) and carry no Home Assistant login; **rotating** the credential
  (regenerate toggle, or a custom Client ID / Secret override) invalidates
  outstanding tokens, but only after the Home Assistant restart the repair
  prompts for. See [SECURITY.md](../SECURITY.md#in-process-server-ha_mcp_tools-in-process-server-entry)
  for the full threat model (unauthenticated consent page, permissive redirect
  URIs, route ownership vs the app).

All three postures ride Home Assistant's own remote access (Nabu Casa / your
reverse proxy) for TLS. If you expose the server to the internet, prefer
`ha_auth`; keep the `none` URL — or a `legacy` Client Secret — strictly private.

The server reaches Home Assistant with the administrator's long-lived access
token entered at setup, stored in the config entry and handed to the server
in memory (never through the Home Assistant process environment). As with every
deployment, that token's Home Assistant permissions define what the server can
do. The component never creates a Home Assistant account or token of its own.

Entries created before this change keep the **HA-MCP Server** account and token
an older release created, for as long as both work. If the token is missing,
revoked or expired, or its account is no longer an active administrator, the
server does not start and the repair **HA-MCP needs an administrator access
token** asks for a new one. You can also switch tokens at any time with
**Replace the administrator access token** in the entry's options. Switching
revokes the token an older release created; its **HA-MCP Server** account stays
until you remove the entry, or until you delete it yourself under **Settings →
People → Users**. Removing the entry deletes only an account an older release
created; it never touches the account your token belongs to.

See [SECURITY.md](../SECURITY.md) for the full threat model.

## Relationship to the File & YAML services entry

The in-process server entry and the **HA-MCP File & YAML Tools** entry are two
config-entry types of the same **HA-MCP Custom Component** (`ha_mcp_tools`). They
are independent: the server works on its own, and most installs never need the
File & YAML entry. Add it only if you enable ha-mcp's opt-in file and YAML editing
tools (feature flags, off by default) — those tools call the privileged services
that entry registers, and that applies to every server type, including an
external app, Docker, or stdio server as well as the in-process server. Add or
remove it at any time from the same **Add Integration** menu (choose **HA-MCP
File & YAML Tools**); it changes nothing about how the in-process server runs.

## First start takes a little longer

The first time Home Assistant loads the component after an install or update,
it downloads and installs the pinned `ha-mcp` package. This can take a minute or
two — occasionally longer — depending on your connection and hardware; the
server starts automatically once the install finishes. Later restarts are fast
because the package is already installed.

## Troubleshooting

**The server won't start.** If the server fails to come up — for example because
the port is already in use — a repair issue titled
**The HA-MCP in-process server failed to start** appears under **Settings →
Repairs**, carrying the specific reason. If a build set in **ha-mcp package
(advanced)** can't be installed, the repair issue is titled **The HA-MCP
in-process server package could not be installed** instead. Fix the cause —
check the Home Assistant log and your network connectivity for an install
failure, or set a different **Server port** for a port conflict — then reload
the entry (save the options, or use **⋮ → Reload**) to retry.

**The integration does not load at all.** Home Assistant installs the server
build each component release pins before it loads the integration. If that
install fails (no network at startup, or a conflict with a package Home
Assistant itself needs), neither entry loads, no repair issue is filed, and the
log reads `Requirements for ha_mcp_tools not found`. Fix the cause the log
names above that line, then restart Home Assistant.

**Nothing happens after updating the component.** Home Assistant loads custom
integration code at startup, so after HACS (or a manual copy) delivers a new
version you must **restart Home Assistant** for the update to take effect.

**Skill guidance is empty after installing from a GitHub tarball.** The **ha-mcp
package (advanced)** field can install from a GitHub tarball URL, but a git
archive excludes submodules — and the bundled skill content ships as a submodule.
A tarball install therefore omits it, so the skill-guidance tools report empty
listings. Pin a PyPI version instead (every PyPI build includes the skill
content); the tarball override is only meant for quick pre-release testing.

**Where the logs are.** The in-process server logs into the normal Home Assistant
log (**Settings → System → Logs**, or `home-assistant.log`). Its working data
lives in `.ha_mcp/` under your Home Assistant config directory.

**The Configure screen shows only a webhook path.** If Home Assistant cannot
determine an external or internal URL, the Configure screen and log show the
webhook path on its own (`/api/webhook/<webhook-id>`); prefix it with your Home
Assistant URL. Set your internal/external URLs under **Settings → System →
Network** so the full URL is shown.
