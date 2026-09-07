# Desktop transport verification

All downloads, extraction, execution, and tests ran on GitHub runners. The
investigation is isolated from normal CI. No existing E2E lane migration or PR
is authorized in this task.

## Verified runs

- Native transport preflight: [34154983863](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34154983863), commit `3a918e67`.
- SDK compatibility and complete helper comparison: [34155594451](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34155594451), commit `53e1b608`.

The first run sent a 102,073-byte JSON-RPC request through a real Chromium
renderer, transferred Electron MessagePorts, and Desktop's extracted stdio
transport. The response crossed two stdout chunks (65,536 and 44,538 bytes).
The original Unicode, percent signs, and template syntax survived exactly.
The process exited cleanly.

The SDK check initialized the existing FastMCP Client through the emulator,
listed tools and resources, read a resource, retrieved a large prompt, and
verified eight concurrent large Unicode/template tool calls. This only proves
client compatibility and message transport, not the original HA dashboard bug.

## Linux versus Windows

The community repository [aaddrick/claude-desktop-debian](https://github.com/aaddrick/claude-desktop-debian)
now packages Anthropic's official Linux application. This investigation used
that official package directly, not the community's modifications.

- Linux Desktop: **1.46388.2**, official amd64 `.deb`.
- Windows Desktop: **1.46388.4**, official x64 MSIX reached from Anthropic's
  [Windows deployment download](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows).
- Linux runtime observed: Electron **42.10.0**, Node **24.18.1**, Chromium
  **148.0.7778.280**.

All 13 selected transport definitions matched under identifier normalization:
stdio subprocess transport, newline read buffer/parser/serializer, MessagePort
transport, forwarding bridge, command/environment preparation, process-group
selection, and process construction. All declared application dependencies
matched too.

Of 430 extracted dependency declarations, 429 matched directly under the same
normalization. The remaining declaration imports a helper under a different
hashed filename. Every declaration and export in that helper matched after
excluding its leading Sentry release/debug identifiers. Artifacts retain the
checksums, comparisons, and source snippets. This is a scoped structural source
comparison, not proof that the complete applications or their operating systems
behave identically.

The inspected builds select the ordinary exec transport for `uvx`. Although
process-group cleanup code is bundled, the selected process-group helper returns
null in this build; the running preflight confirmed `process_group: false`.
Windows-specific branches, including environment and process creation, are
present in the shared code and are not executed by the Linux runtime.

## What is emulated

The runner launches the official Linux executable with a replacement test
bootstrap in its runner-local `app.asar`. That bootstrap drives unchanged
extracted transport declarations and their dependency initialization, using a
real hidden Chromium window and native `MessageChannelMain` ports. It can spawn
either `uvx --from <checkout wheel> ha-mcp` or `uvx --from
fastmcp-remote==4.0.3 fastmcp-remote <URL> --auth none`.

A test renderer supplies requests instead of the authenticated cloud chat UI.
The original transport's message serialization and forwarding execute, but its
application logger is replaced by test instrumentation; original Winston log
files and toast UI are not reproduced. User login, cloud tool selection,
Windows OS pipes, and the reporter's unspecified Desktop version are not tested
by the Linux harness. Do not call this a complete logged-in Desktop reproduction.

The original sanitized dashboard is stored byte for byte in this directory.
HAOS dashboard runs exercise omitted/false MandatoryBPS, two protocol versions,
single/dual launch channels, fresh processes, and repeated edits with independent
readback and restoration. Standalone mode removes baked HA-MCP integrations and
asserts they are not loaded; app/component modes use a real HA-hosted MCP server.
The dual case is a deliberate extra-channel stress test, not a claim that every
Desktop session launches two servers by default.

## Windows release preceding the report

[Run 34155972855](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34155972855)
compared Windows **1.44121.2** (September 2) with the tested Linux 1.46388.2.
The reporter did not supply their Desktop version, so this is a period-appropriate
comparison candidate, not confirmed installed-client provenance.

Eleven of the thirteen selected transport definitions match. The newer forwarding
bridge adds an optional `onServerMessage` callback; the older bridge lacks it.
The newer command resolver also checks the resolved command before launching it.
The pipe transport, buffer/parser/serializer, MessagePort transport, environment
selection, process specification, and buffer limit match. The application-level
startup coordinator also differs and is not part of the extracted transport
harness. Package dependency declarations match. These observations narrow the
shared portion; they do not equate either app's full lifecycle with the emulator.
