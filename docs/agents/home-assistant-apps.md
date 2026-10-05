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
package version sourced from `pyproject.toml`.

Release automation synchronizes only the version and changelog into the stable
flavor: the `update-addon-config` job owns the version, and
`semver-release.yml`'s `Copy changelog to addon directory` step owns the
changelog. It does not synchronize functional configuration. When a non-beta
capability should exist in both flavors, edit both `config.yaml` files in the
same pull request. This includes `ingress`, `ports` and `host_network`.
Issue #2083 is the precedent: assuming the release pipeline would mirror
functional configuration left `ingress` off the stable app.

The `options:` and `schema:` blocks of both `config.yaml` files and
`homeassistant-addon/app_options.json` are generated from the `Settings`
model: a field whose `Setting` metadata carries an `AppOption` is an app
option, and `AppOption.flavors` says which flavors declare it. To add or
change an option, edit the field in `src/ha_mcp/config_settings.py`, then run
`python scripts/generate_app_options.py` and
`python scripts/generate_locales.py`. `start.py` exports each option from
`app_options.json`; it cannot import `ha_mcp` for it, because importing the
package builds the server's settings before the options are exported. Every
option needs a section in the stable `DOCS.md` and a row in the dev
`DOCS.md` table. Beta-only keys are declared in the dev flavor only; see
[`docs/beta.md`](../beta.md).

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
