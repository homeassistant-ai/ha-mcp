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

Test on `server=embedded` with the File & YAML Tools entry in place first (the
defaults): the server running in-process inside the component, with the
filesystem tools beside it, is ha-mcp's main install. Use `standalone` or `app`
for a change specific to that mode.

`no_component=true` removes the component's File & YAML Tools entry, like the
no-tools E2E lanes. `strict_bps` (default on, as agents meet it) sets strict
best-practices mode for the standalone and embedded servers. `ha_image`
overrides the Docker image (default: `ghcr.io/home-assistant/home-assistant:stable`).
Pass an explicit version tag to test an older Core release, or `:beta` to test
the current beta. This dev default is independent of the branch's E2E pin. `minutes`
defaults to and is capped at 330. A HAOS run builds the HAOS image the first
time the image build files, the test config or the proxy app change, and saves
it to the fork's cache for later runs. A branch whose component differs from
every cached image reuses one with the same base and applies its own component
at boot with a Core restart.

Run it **from your fork only**. The job refuses to run in
`homeassistant-ai/ha-mcp`, whose runners are shared CI capacity.

1. Enable Actions on your fork and sync its `master`, since GitHub only
   dispatches workflows that exist on the default branch.
2. Make a throwaway key pair for this run. The tunnel URLs are published
   only encrypted to its public key, so nothing secret goes to GitHub and
   any machine can start a run:
   ```bash
   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out devenv-key.pem
   ```
3. Start it against any branch of your fork, passing the public key:
   ```bash
   gh workflow run dev-ha-env.yml -R <you>/ha-mcp -f track_ref=<branch> \
     -f platform=docker -f server=embedded \
     -f public_key="$(openssl pkey -in devenv-key.pem -pubout | openssl base64 -A)"
   ```
4. Once the run reaches "Keep running", download and decrypt the URLs:
   ```bash
   gh run download <run-id> -R <you>/ha-mcp -n dev-ha-env-urls
   openssl pkeyutl -decrypt -inkey devenv-key.pem -pkeyopt rsa_padding_mode:oaep -in dev-ha-env-urls.enc -out dev-ha-env-urls.txt
   cat dev-ha-env-urls.txt
   ```
   `HA:` is the HA UI and API. `MCP:` is your branch's server (streamable
   HTTP, path included), ready to add to an MCP client.

   Test through `MCP:`, the way an agent uses the server: add it to your
   client as a regular MCP server, or, when the agent's client cannot load a
   new server mid-session, drive it one call at a time through a shim that
   sends `tools/list` and `tools/call` to that URL.
   [`.github/dev-ha-env/mcpshim.py`](../.github/dev-ha-env/mcpshim.py) is an
   example; agents can modify it as needed:
   ```bash
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt tools
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt describe ha_search
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt call ha_search '{"query": "kitchen"}'
   ```
   Do the setup and the checks with the tools as well. Creating or verifying
   state through `HA:`'s REST API routes around the tools under test, so a
   gap in them goes unnoticed.
5. Iterate the way you would update ha-mcp on any Home Assistant. The run
   stays on the commit it started from, with developer mode on:
   - Embedded server: push, then call
     `ha_dev_manage_server(action="update_source",
     pip_spec="ha-mcp @ git+https://github.com/<you>/ha-mcp@<commit>")`.
     The server reinstalls and comes back within a few minutes;
     `ha_dev_manage_server(action="info")` shows the spec it runs. The run
     starts from a wheel built from your branch's checkout.
   - Component: add your fork to HACS once with
     `ha_manage_hacs(action="add_repository", repository="<you>/ha-mcp",
     category="integration")`, redownload your branch with
     `ha_manage_hacs(action="download", repository_id=<id>, version="<branch>")`,
     then restart Core with `ha_restart`. Right after a push, HACS can still
     fetch the previous commit: wait a minute and read a changed `.py` file
     with `ha_read_file` before restarting. The component keeps its pending
     version number across dev builds, so HACS's `installed_version` (the
     branch) is what names the build.
   - Standalone server or app: start a new run on the new commit.

   The job log's `STATUS` lines show the commit the run started from and any
   boot error. Home Assistant stays up if the embedded server fails at boot;
   update the server as above.

   The instance is a normal HA you can change. On HAOS, change the Core
   version from Settings → System → Updates, or run
   `ha core update --version 2026.10.0` in the Advanced SSH app's terminal;
   apps, integrations and the component install and uninstall as usual. Docker
   HA has no update mechanism, so start the same run again with another
   `ha_image`; the new run replaces the old one.
6. **Cancel the run when you're done.** It does not stop on its own until
   `minutes` runs out, and it holds a runner the whole time:
   ```bash
   gh run cancel <run-id> -R <you>/ha-mcp
   ```

Anyone who has a tunnel URL has admin on that instance through the public test
credentials. Don't share the URLs, and don't put anything real in it.
