# Home Assistant app development

Read this document before changing the stable or development Home Assistant app
configuration, publishing behavior, or webhook-proxy app. General release
automation is documented in [GitHub workflow reference](github-workflow.md).

## Main server app

Repository recognition requires the root `repository.yaml`. The two app
flavors are independent:

- `homeassistant-addon/`: stable, slug `ha_mcp`.
- `homeassistant-addon-dev/`: development, slug `ha_mcp_dev`.

Each has its own `config.yaml`. Stable's version must match the released
package version when published.

Release automation synchronizes version and changelog data into the stable
flavor; it does not synchronize functional configuration. When a non-beta
capability should exist in both flavors, edit both `config.yaml` files in the
same pull request. This includes `ingress`, `ports`, `host_network`,
`options`, and `schema`. Beta-only keys are the documented exception; see
[`docs/beta.md`](../beta.md) and the note in the app configuration.

Both app flavors select architecture-specific images through explicit
`version:` pins. Do not infer their state from the general server container's
`:latest` tag.

## Webhook Proxy app

Before any webhook-proxy change, read
[`homeassistant-addon-webhook-proxy/AGENTS.md`](../../homeassistant-addon-webhook-proxy/AGENTS.md).
That scoped document owns the two flavors, mutual exclusion, version rules,
tests, and promotion transform.

The stable proxy tree is not edited directly during normal development. Every
code and documentation change lands in
`homeassistant-addon-webhook-proxy-dev/` with its required version bump, is
tested on the development channel, and reaches stable through the manual
promotion workflow. The stable contributor `AGENTS.md` is the documented
exception because it serves both flavors.

Do not duplicate the stable instructions into the dev stub. The dev
[`AGENTS.md`](../../homeassistant-addon-webhook-proxy-dev/AGENTS.md) routes
back to the shared owner.

## Publishing references

- [Home Assistant app documentation](https://developers.home-assistant.io/docs/apps)
- [Development channel](../dev-channel.md)
- [Beta features](../beta.md)
- [In-process server](../in-process-server.md)
