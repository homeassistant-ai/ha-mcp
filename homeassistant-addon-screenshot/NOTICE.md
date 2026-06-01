# Attribution

The **HA MCP Dashboard Screenshot Engine** add-on (`ha_mcp_screenshot`) is
derived from the **Puppet** add-on by Paulus Schoutsen (balloob):

- Source: https://github.com/balloob/home-assistant-addons (`puppet/`)
- License: Apache License 2.0 (see [`LICENSE`](./LICENSE))

The screenshot engine itself (the `ha-puppet/` Node service that drives
headless Chromium, injects `hassTokens`, and serves PNGs over HTTP) is
vendored largely verbatim from that project and retains its Apache-2.0
license.

## Modifications by the ha-mcp project (homeassistant-ai/ha-mcp)

- Rebranded the add-on metadata (`config.yaml`) as `ha_mcp_screenshot`.
- Repackaged as the opt-in screenshot engine behind the ha-mcp
  `ha_get_dashboard_screenshot` tool and the `include_screenshot` /
  `return_screenshot` parameters on the dashboard config tools.
- Built locally by the Supervisor (no `image:` key), matching the
  ha-mcp webhook-proxy add-on packaging.
- Modified `ha-puppet/const.js` to auto-authenticate via the add-on's
  Supervisor token (`homeassistant_api: true`) when no `access_token`
  option is set — so it works without a manually-pasted long-lived token;
  a configured `access_token` still overrides. Made the add-on options
  optional in `config.yaml`/schema accordingly.
