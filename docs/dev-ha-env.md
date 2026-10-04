# Live dev environment

`.github/workflows/dev-ha-env.yml` runs a throwaway Home Assistant on a GitHub
runner and exposes it, plus your branch's ha-mcp server, through temporary
Cloudflare tunnels. Use it to drive a branch against a real HA without Docker
and without touching your own instance. It boots HA with the E2E suite's own
bring-up and config (user `mcp` / `mcp`, token `TEST_TOKEN` in
`tests/test_constants.py`). It is separate from the test suite: nothing in CI
runs it, and its runner script (`.github/dev-ha-env/hold.py`) is copied into
the tracked branch's E2E directory only for the run.

| `platform` | `server` | What runs |
|---|---|---|
| `docker` | `standalone` | HA container; your branch's server runs on the runner |
| `docker` | `embedded` | HA container; the server runs inside the component |
| `haos` | `standalone` | HAOS VM; your branch's server runs on the runner |
| `haos` | `embedded` | HAOS VM; the server runs inside the component |
| `haos` | `app` | HAOS VM; the server runs as the app (add-on) |

`no_component=true` removes the component's File & YAML Tools entry, like the
no-tools E2E lanes. `strict_bps` (default on, as agents meet it) sets strict
best-practices mode for the standalone and embedded servers. `ha_image`
overrides the Docker image (default: the branch's pinned E2E image). `minutes`
defaults to and is capped at 330. A HAOS run builds the HAOS image the first
time its inputs change (the image build files, the test config or the
component) and saves it to the fork's cache for later runs.

Run it **from your fork only**. The job refuses to run in
`homeassistant-ai/ha-mcp`, whose runners are shared CI capacity.

1. Enable Actions on your fork. Then set a passphrase as a repository secret.
   The tunnel URLs are published only encrypted with it:
   ```bash
   gh secret set DEV_HA_ENV_PASSPHRASE -R <you>/ha-mcp
   ```
2. Sync your fork's `master` first, since GitHub only dispatches workflows
   that exist on the default branch. Then start it against any branch of your
   fork:
   ```bash
   gh workflow run dev-ha-env.yml -R <you>/ha-mcp -f track_ref=<branch> \
     -f platform=haos -f server=app
   ```
3. Once the run reaches "Keep running", download and decrypt the URLs. This
   needs OpenSSL 1.1.1+: Git Bash on Windows, any Linux, Homebrew `openssl`
   on macOS.
   ```bash
   gh run download <run-id> -R <you>/ha-mcp -n dev-ha-env-urls
   openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in dev-ha-env-urls.enc
   ```
   OpenSSL prompts for the passphrase. That keeps it out of your shell history.
   `HA:` is the HA UI and API. `MCP:` is your branch's server (streamable
   HTTP, path included), ready to add to an MCP client.
4. Push to the branch to iterate. Every 20 seconds the runner picks up new
   commits and applies them the way a user's update would:
   - A server change restarts the standalone server. The embedded server gets
     a wheel built from the commit, as `ha_dev_manage_server(update_source)`
     installs one. The app gets a version-bumped source and Supervisor's app
     update.
   - A change under `custom_components/ha_mcp_tools/` replaces the component,
     as HACS does, and restarts Home Assistant.

   The job log's `STATUS` lines show the running commit and any update error.

   The instance is a normal HA you can change. On HAOS, change the Core
   version from Settings → System → Updates, or run
   `ha core update --version 2026.10.0` in the Advanced SSH app's terminal;
   apps, integrations and the component install and uninstall as usual. Docker
   HA has no update mechanism, so start the same run again with another
   `ha_image`; the new run replaces the old one.
5. **Cancel the run when you're done.** It does not stop on its own until
   `minutes` runs out, and it holds a runner the whole time:
   ```bash
   gh run cancel <run-id> -R <you>/ha-mcp
   ```

Anyone who has a tunnel URL has admin on that instance through the public test
credentials. Don't share the URLs, and don't put anything real in it.
