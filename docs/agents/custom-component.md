# Custom component development

Read this document before changing `custom_components/ha_mcp_tools/` or a
server feature that depends on the component. The HACS component and the
`ha-mcp` server ship through separate installation paths, so compatibility
must hold in both update directions.

## Release ZIP

Releases provide `ha_mcp_tools.zip` for integration installation. Its root
contains the integration files (`manifest.json`, `__init__.py`, translations,
and the remaining tracked component files), without a repository or domain
folder. Build it with `uv run -s scripts/build_component_zip.py`. The builder
reads tracked **working-tree** files so release-time version/server stamps
are included; it excludes untracked files and bytecode.

Stable and dev publishing build the ZIP after stamping and attach it while
the GitHub release is a draft. The mirror copies and verifies the upstream
asset instead of rebuilding it. Before tagging, sync compares every ZIP member
and its bytes with the committed, stamped component snapshot, then commits the
verified asset digest alongside its source metadata. Copying requires that pinned
digest, so a replaced dev release cannot silently change a mirror tag's payload.
Do not replace an existing asset just to refresh
its download count; that count is downloads of one release asset, not users.

Dev and mirror creation pass the ZIP to `gh release create`, which stages the
upload before publishing and attempts cleanup on failure. Mirror retries resolve their
own releases by database ID so leftover drafts are recoverable. A complete
mirror asset matching the tag's digest is reused even if its upstream dev release was pruned; incomplete
uploads are replaced before a recovered draft is published. The upstream
source lookup still requires a published release.

HACS/Marketplace select the asset through `zip_release` and `filename` in
`hacs.json`. Enable these only after **every selectable stable, dev and mirror
release, including older versions**, has the ZIP. HACS may reuse the latest
manifest for an older version selected at first install; checking only the
latest release is insufficient. Keep the activation PR draft until this is
verified against both stable and pre-release version choices. Backfill missing
assets from each exact release checkout, preserving its manifest/pin; copy the
same bytes to its mirror release. Never replace a usable counted asset.

Stable SemVer creates a draft with the stamped ZIP; Release
Publish publishes PyPI, verifies an available pure Python wheel for the Python
versions declared by the released project's metadata, and checks the ZIP,
then publishes the draft and advances `stable`. Release Publish supplies
`hacs.json` and `pyproject.toml` from its tag checkout. The finalizer independently
compares the tracked component directory with that tag, then downloads the
asset by ID and checks its digest and members. These checks precede both GitHub
writes. Legacy archive
releases that do not select a ZIP keep their recovery path.
Do not wait for PyPI inside SemVer: the PyPI workflow starts only after SemVer
completes. The site deploy
and the mirror consume the finalized-tag artifact from Release Publish, even
if a later Docker or MCP Registry job fails. They ignore a superseded marker.
Find drafts through the authenticated, paginated release list: GitHub's
release-by-tag endpoint only guarantees published releases. Keep the lookup
unbounded by a fixed release count so older recovery targets remain reachable.

Release Publish takes the exact tag artifact from the triggering SemVer run;
gated runs provide none. Moving master cannot change its selection. Runs are
serialized across finalization and container publishing. The alias push uses
the exact previously observed tag-object SHA as its force-with-lease condition;
a concurrent operator change is never overwritten. Tag ancestry compares
immutable peeled commit SHAs while the alias preserves the annotated tag object.
An old retry cannot move the alias backwards, and a partial Docker rerun checks
that its release is still current before promotion. Independent manual release
publication must not race this workflow; historical workflow revisions do not
inherit new concurrency controls. Recover through the current publisher.

A manual stable mirror recovery supplies `release_publish_run_id` from a
finalized Release Publish run on master. Before reading its finalized-tag artifact,
validation checks the run's workflow ID against `release-publish.yml`, its master
branch and a publisher event (`workflow_run` or `workflow_dispatch`). PR runs and
failed metadata lookups are refused; a later Docker/MCP failure does not invalidate
successful finalization. Recovery uses the same artifact and current-alias checks
as the automatic path, never the latest public release. A public release left by
a failed alias lease is not sufficient: retry the current publisher first.
The resolved tag must still match the raw stable alias both before staging
the snapshot and again at the final mirror-tag step.

Prerequisite failures perform no GitHub writes. An unconfirmed publication
response stops before the alias write; inspect the release and retry to
reconcile its state. If publication succeeds but the alias lease fails, the
verified, installable release stays public, this run leaves the alias alone,
and Docker publishing is skipped.
Inspect the operator's change before retrying the current publisher. Returning
the release to draft cannot undo downloads or publication events and is not a
transactional rollback. A public release with the correct alias is a no-op, and
existing assets are not replaced. The finalizer reads the
tag's manifests, so pre-ZIP tags keep their legacy archive behavior on recovery.
The wheel check does not resolve or qualify all transitive Core dependencies;
their existing compatibility and no-stomp checks remain authoritative.

The container E2E fixture installs this artifact before HA boots, so existing
component scenarios exercise the ZIP. The existing update-path scenarios retain
their released-component case and install the working-tree case from the ZIP.
That case also reinstalls the ZIP over configured HA, restarts HA, and checks
entry preservation, the selected server version and a real MCP read. It does
not claim an old-version component migration or a Marketplace UI click test.
For a focused GitHub runner execution, dispatch `e2e-tests.yml` on a test fork
with `update_path_only=true`.

### Recover a stable tag pushed before its draft was created

If uv setup or ZIP creation fails after SemVer pushes its tag, a fresh SemVer
dispatch may report no new release. Recover the same tag instead of cutting a
new version. Pause other release cuts during recovery, confirm that the tag is
the current stable-release candidate and that its GitHub release is absent.
From the current publisher checkout, use the original tag's component and notes:

```bash
set -euo pipefail
TAG=vX.Y.Z
REPO=homeassistant-ai/ha-mcp
git fetch origin master --tags
test "$(git describe origin/master --tags --abbrev=0 --match 'v[0-9]*' --exclude '*dev*')" = "$TAG"
git worktree add --detach worktree/release-recovery "$TAG"
uv run -s scripts/build_component_zip.py --component-dir worktree/release-recovery/custom_components/ha_mcp_tools --output local/release-recovery/ha_mcp_tools.zip
uv run -s scripts/extract_release_notes.py --version "$TAG" --changelog worktree/release-recovery/CHANGELOG.md --out local/release-recovery/notes.md
test -s local/release-recovery/notes.md
gh release create "$TAG" local/release-recovery/ha_mcp_tools.zip --repo "$REPO" --verify-tag --draft --title "$TAG" --notes-file local/release-recovery/notes.md
gh workflow run release-publish.yml --repo "$REPO" --ref master
```

The current Release Publish workflow selects that existing tag, publishes its
PyPI package with `skip-existing`, then verifies and finalizes the recovered
draft. If the draft already exists, inspect it and upload only a missing ZIP;
do not recreate it or overwrite a usable counted asset. Confirm finalization
and its finalized-tag artifact before recovering the mirror. This is a manual
recovery path, not a claim that rerunning SemVer repairs a stranded tag.

## Version cycle

The component shares the server's version and pins the server build it runs
(#2427). `manifest.json`'s `version`, its `ha-mcp==` requirement and
`COMPONENT_VERSION` in `const.py` always equal `pyproject.toml`'s version.
Never edit them by hand: semantic-release stamps all four in each release
commit (`version_variables` in `pyproject.toml`), and the PR **Component
Version Gate** fails a pull request that moves any of them.

HACS installs the component from this repository; the
`homeassistant-ai/ha-mcp-integration` mirror stays for installs made from it and
receives the same releases (#2763). Releases follow from that:

- **Stable:** the SemVer Release commit carries the stamp, so this
  repository's `vX.Y.Z` release is the component release. After it, the mirror
  sync snapshots the release tag's component and tags the mirror `vX.Y.Z`
  once PyPI serves `ha-mcp==X.Y.Z`. A server-only release is therefore also a
  component release.
- **Development:** **Publish Dev Channel** stamps the component with the next
  release number and its `ha-mcp-dev` build on a commit only its
  `v<next>.dev<N>` pre-release tag references, so master keeps its stable
  pin. After it, the mirror sync stamps the
  snapshot of the commit it built with the next release number (what
  semantic-release would cut) and pins the exact build it uploaded,
  `ha-mcp-dev==<next>.dev<N>` (the version `scripts/dev_version.sh` gave
  that run, handed over as its `dev-version` artifact), and tags it
  `v<next>-dev.<N>` as a HACS pre-release once PyPI serves that build. As
  here, the stamp is on a commit only that tag references: the mirror's `main`
  carries master's component unstamped. Both
  repositories keep their newest five dev pre-releases, because HACS reads a
  repository's 30 newest releases and installs the default branch when none
  of them is stable (#2881).

The component never reports a dev suffix: released servers parse the version
segment by segment as integers, so the suffix lives only in the pre-release tag
and the pin.

When a change adds a component service or argument that the server depends
on, raise `MIN_COMPONENT_VERSION` in `src/ha_mcp/tools/tools_filesystem.py`,
in the same PR, to the release that will carry it: the number
`scripts/dev_version.sh` prints without its `.devN` suffix (it computes the
next release from `master` on any branch, so count the PR's own commit type:
a `feat:` on a `master` that only has fixes pending raises the minor).

Every development pre-release cut while that release is pending reports the
same plain number, so the floor cannot tell a pre-release that has the new
service from an earlier one that lacks it. Only an external server (app,
Docker, pip) is affected; the in-process server runs the build its component
release pins. This gap is accepted: pre-release users update the component
from HACS.

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

For component behavior changes, follow the
[repository live-testing requirement](../../AGENTS.md#testing-and-verification)
before merge, using the user's choice of the
[live dev environment](../dev-ha-env.md) or another live Home Assistant.
The dev environment runs a fork branch's component and server against a
throwaway Home Assistant on a GitHub runner, so live
validation does not require merging or publishing a release first. Start
with the embedded server and File & YAML Tools entry, then exercise the
other affected topologies described below. Follow the environment guide for
setup, iteration, and cleanup when using it; this manually dispatched
environment is separate from automated CI.

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
makes upstream calls with an administrator's long-lived access token
(`server_credentials.py`): one the user pastes, or one issued at setup from the
login of an account the user chose, which the component revokes on a switch or
removal. The component never creates an account: entries from older releases
keep their provisioned pair while it works,
and a missing or refused credential files the fixable `server_token_needed`
repair instead. The token is passed in memory, not through the Home Assistant
process environment; disabling the entry stops the server, and so does the
entry's **Access** switch (disabled by default) while the entry stays loaded.
Removing the entry deletes only an account an older release created, never the
user's.

The settings panel reverse-proxies the web UI through Home Assistant. Browser
access uses a short-lived HttpOnly session cookie issued to an authenticated
administrator. Every request revalidates that the session still maps to an
active administrator. Never expose the loopback secret path or place a token or
secret in a URL.

The full threat model and route-ownership rules are in
[`SECURITY.md`](../../SECURITY.md).
