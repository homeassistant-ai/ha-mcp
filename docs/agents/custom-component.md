# Custom component development

Read this document before changing `custom_components/ha_mcp_tools/` or a
server feature that depends on the component. The HACS component and the
`ha-mcp` server ship through separate installation paths, so compatibility
must hold in both update directions.

## Version cycle

The component version in `manifest.json` and `COMPONENT_VERSION` in
`const.py` must stay identical. The version rides the stable release cycle;
do not bump it once per pull request or push.
`test_manifest_version_parity` in
`tests/src/unit/test_component_ws_search.py` enforces the lockstep.

Compare `master` with the last stable release:

```bash
git show stable:custom_components/ha_mcp_tools/const.py \
  | grep COMPONENT_VERSION
```

Apply these rules:

- If `master` is level with stable, bump once—patch by default—to open the
  pending version.
- If `master` already leads stable, do not bump; the change rides in that
  pending version.
- Raise an existing pending version only to escalate the required bump level,
  such as patch to minor. Do not create never-shipped intermediate versions.
- The PR Component Version Gate requires a changed component to lead the
  mirror's released stable version. The mirror sync separately rejects
  content drift under an already-tagged component version, on the
  push-to-master leg right after a merge and again at release time.
  In the PR gate, equal means a bump is needed to open the pending version;
  behind means a stale tree or bad merge resurrected an older version.

The mirror drift check prevents changes from being stranded under a version
that already shipped and therefore has no new installable release. The gap it
catches is a pull request opened while a version was pending but merged after
that version became stable: a PR check never reruns for an external event,
so the recorded green stands and the merge goes through. The push-to-master
sync leg then fails the merge commit's own run with the bump instruction
(on every push, so a rerun after the snapshot already landed still fails),
and the stable-tag step repeats the check at release time as the backstop.

One exception overrides the shared pending-version rule: if a change adds a
component service or argument that the server depends on, open a fresh pending
component version even when one already exists, then raise
`MIN_COMPONENT_VERSION` in `src/ha_mcp/tools/tools_filesystem.py` to that
same version. The floor must identify only builds that contain the capability.
Never use a released or previously opened pending version that also exists
without the new behavior; callers on that build would pass the gate and then
hit a raw missing-service failure.
`get_caller_token` reports the component manifest version used by this gate.
The issue #1946 failure is the precedent: the floor was set to an already
shipped 1.1.0, so builds reporting 1.1.0 existed both with and without the
required behavior and the gate could not distinguish them.

## Compatibility

A new component can run against an older released server because HACS and the
server package update independently. Do not remove or tighten an existing
service schema without a compatibility shim the previous server can still
satisfy. Remove that shim only after the matching server version becomes the
minimum supported component consumer.
The version gate cannot protect this direction because the older server is the
caller and does not know to demand the new component.

A component path cannot be fully exercised by pre-merge CI. After merge,
live-test it promptly on the development server before the next stable cut.

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
