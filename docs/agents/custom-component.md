# Custom component development

Read this document before changing `custom_components/ha_mcp_tools/` or a
server feature that depends on the component. The HACS component and the
`ha-mcp` server ship through separate installation paths, so compatibility
must hold in both update directions.

## Version cycle

The component shares the server's version and pins the server build it runs
(#2427). `manifest.json`'s `version`, its `ha-mcp==` requirement and
`COMPONENT_VERSION` in `const.py` always equal `pyproject.toml`'s version.
Never edit them by hand: semantic-release stamps all four in each release
commit (`version_variables` in `pyproject.toml`), and the PR **Component
Version Gate** fails a pull request that moves any of them.

Releases follow from that:

- **Stable:** after **SemVer Release**, the mirror sync snapshots the release
  tag's component and tags the mirror `vX.Y.Z` once PyPI serves
  `ha-mcp==X.Y.Z`. A server-only release is therefore also a component
  release.
- **Development:** after **Publish Dev Channel**, the mirror sync stamps the
  master snapshot with the next release number (what semantic-release would
  cut) and a pin on the `ha-mcp` dev build just published
  (`<next>.dev<N>`, from `scripts/dev_version.sh`), and tags it
  `v<next>-dev.<N>` as a HACS pre-release.

The component never reports a dev suffix: released servers parse the version
segment by segment as integers, so the suffix lives only in the pre-release tag
and the pin.

When a change adds a component service or argument that the server depends
on, raise `MIN_COMPONENT_VERSION` in `src/ha_mcp/tools/tools_filesystem.py`
to the release that will carry it: the version
`uvx --from python-semantic-release==<pinned> semantic-release --noop version --print`
prints on `master` after the change merges (the pinned version is in
`scripts/dev_version.sh`). Development pre-releases report that same number,
so they pass the floor; every earlier build reports a lower one.

## Compatibility

The embedded server always runs the build its component release pins, but an
external server (app, Docker, standalone) talking to the component through
the tools entry still updates independently, so a new component can run
against an older released server. Do not remove or tighten an existing
service schema without a compatibility shim the previous server can still
satisfy. Remove that shim only after the matching server version becomes the
minimum supported component consumer.
The version gate cannot protect this direction because the older server is the
caller and does not know to demand the new component.

A component path cannot be fully exercised by pre-merge CI. After merge,
live-test it promptly on the development server before the next stable cut.
Before merge, the [live dev environment](../dev-ha-env.md)
runs a branch's component and server against a throwaway HA from a fork.

## Dependencies shared with Core

The embedded server installs into Home Assistant's Python environment using
Core's `package_constraints.txt`. The standalone `uv.lock` is not the embedded
installation contract. For libraries shared with HA, keep package requirements as
bounded compatibility ranges that admit supported Core versions. Preserve the
standalone lock's selected versions when widening a range; widen or raise a
floor only with compatibility evidence. Pydantic follows its major-version
boundary; HTTPX is pre-1.0, so its minor-version boundary remains capped.

Dependabot continues updating Pydantic and HTTPX for standalone, Docker, and
app installations. Their standalone lock versions may advance independently
of Core's pins while the package requirements still admit those pins. Review
requirement changes against supported Core versions; an incompatible floor
must fail alignment checks rather than silently strand embedded users.
Renovate advances the Core E2E image, not these package requirements. Never
fix an embedded resolver failure by dropping HA's constraints or eagerly
upgrading its shared packages.

Fast Checks verifies direct dependency alignment against both the current
Core image and the `hacs.json` minimum, without adding E2E lanes. Existing
current-Core embedded E2E on x64/ARM64 covers transitive installation and
preinstall/runtime replacement of HA-governed packages; nightly beta E2E
provides advance notice of upcoming Core regressions.

The minimum-version check does not prove transitive installability or runtime
behavior there. These checks also do not cover every intervening release,
every installed third-party integration, or future API compatibility.
Keep the HACS minimum honest; passing current-Core E2E alone does not justify
raising a dependency floor past versions older supported Core releases need.

## Two entries, one command surface

The integration has two config-entry types:

- **HA-MCP Server** runs the server in-process and exposes it through a Home
  Assistant webhook.
- **File & YAML Tools** registers privileged filesystem and YAML services.

Both entries register the shared `ha_mcp_tools/*` WebSocket command surface.
An external server therefore reaches in-process capabilities through the tools
entry alone. Every shared handler must read live Home Assistant state rather
than tools-entry `hass.data`, or a server-entry-only installation breaks.

`async_register_command` is idempotent, and Home Assistant provides no
unregister operation. Commands remain registered after an entry unload until
Home Assistant restarts. Do not attempt to tear them down; do clear that
entry's `hass.data` caches so the next setup reads storage again.

Privileged filesystem and YAML services remain tools-entry-only. The
`ha_mcp_tools/info` command answers for either entry, so the server gates
those services on the additive `tools_services` handshake field—not the
general capability list.

When changing component-backed behavior:

- Update the component producer, server consumer, capability gate, and legacy
  fallback in the same pull request.
- Serve shared commands identically from both entry types.
- Gate privileged or beta behavior on an explicit tools-entry signal.
- Exercise both topologies, including the no-tools lanes described in
  [`tests/AGENTS.md`](../../tests/AGENTS.md#no-tools-lanes-e2e_no_tools_entry1-2292).

## Embedded server security

The in-process server accepts active human Home Assistant administrators and
uses a dedicated component-provisioned admin token for upstream calls. The
token is passed in memory, not through the Home Assistant process environment;
removing the entry revokes it, and disabling the entry stops the server.

The settings panel reverse-proxies the web UI through Home Assistant. Browser
access uses a short-lived HttpOnly session cookie issued to an authenticated
administrator. Every request revalidates that the session still maps to an
active administrator. Never expose the loopback secret path or place a token or
secret in a URL.

The full threat model and route-ownership rules are in
[`SECURITY.md`](../../SECURITY.md).
