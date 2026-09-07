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
by the Linux harness. The separate Windows preflight below covers only a native
pipe echo exchange. Do not call this a complete logged-in Desktop reproduction.

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

## Completed HAOS Desktop batches

None of these batches reproduced the reported hang or identified a cause or a fix.

| HA-hosted server | GitHub run / tested commit | Cases passed | Target writes | Slowest target write |
| --- | --- | ---: | ---: | ---: |
| App | [34155210859](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34155210859), `34d82df1` | 8 | 264 | 141 ms |
| Component | [34155593237](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34155593237), `53e1b608` | 8 | 264 | 531 ms |
| Standalone stdio | [34157268528](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34157268528), `76d5457e` | 8 | 264 | 60 ms |

Each batch also completed 264 restorations and eight initial dashboard creations,
with 32 Desktop processes exiting cleanly. Each target write was preceded by a
read through Desktop and independently checked afterward. The additional channel
completed 220 dashboard reads in the app batch, 218 in the component batch, and
100 in the standalone batch.
Both protocol versions negotiated as requested. No cases were skipped. Artifacts
include JUnit, source SHAs, request/result timings and transport traces.

These runs used the exact large transform and attached dashboard. The small-edit
variant assigned `mdi:music-box`, differing from the reporter's final string
`mdi:music-box-multiple`; later standalone attempts use the exact latter value.
The two strings exercise the same one-key shape, but are not byte-identical.

Standalone setup attempts initially exposed harness mistakes, not the reported
hang: run 34155208417 removed integrations too late; run 34156353394 successfully
booted bare HA but received an immediate BPS_ACKNOWLEDGMENT_REQUIRED during initial
dashboard creation. The experiment now orders removal before boot and explicitly
sets ENABLE_STRICT_MANDATORY_BPS=false in every standalone child environment.

Run 34157052181 progressed through standalone Desktop reads and writes, then
stopped at a harness assertion that incorrectly required a hash change for the
exact static edit. The supplied dashboard already has that icon. The corrected
assertion checks the expected final icon and allows its hash to stay unchanged.
This immediate assertion failure was not a timeout reproduction.

The completed standalone batch passed all eight cases with zero skips, failures
or errors (JUnit: 218.944 seconds). Offline removal recorded both MCP integration
domains, and each case verified that HA had no loaded HA-MCP component or services
and no app/component MCP endpoint. The Desktop-side uvx process supplied HA-MCP.
All 32 Desktop processes exited with code zero. Its exact static edit was a
verified no-change write against the supplied baseline; its large transform
changed the section count from three to four and was restored afterward.

Across the three completed Desktop/HAOS batches there were 792 successful target
write calls and 792 preceding dashboard reads through Desktop, plus restorations,
independent readbacks and the additional-channel reads above. These are short
instrumented batches. The maintainer's prior Windows reproduction in #1644 was
two hangs followed by hundreds of successful calls, so this count cannot exclude
the reported intermittent failure.

## Native Windows transport preflight

[Run 34158113370](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34158113370),
commit `3e5409ec`, passed on a Windows GitHub runner. It extracted 430 declarations
from official Windows Desktop 1.46388.4 and ran them in stock Electron 42.10.0
(Node 24.18.1, Chromium 148.0.7778.280), matching the observed Linux runtime
versions. The echo child used the runner's Python 3.12.10. This used Electron's
Windows binary, not the complete Desktop executable or the reporter's Python
installation.

The 102,073-byte request preserved Unicode, percent signs and Jinja syntax. The
response arrived over native Windows child-process stdout in chunks of 65,536
and 44,539 bytes; its parsed JSON-RPC representation was 102,066 bytes. The extra
raw wire size comes from JSON escaping and line endings. The test asserted exact
payload equality and clean process exit. It was one echo round trip, not a
Windows HAOS dashboard batch or an intermittent-failure stress result.

The external test controller talks to the Windows GUI emulator over a loopback
socket. Inside the emulator, requests still traverse real Chromium MessagePorts
and the unchanged extracted Desktop stdio implementation to a Python child.
The socket replaces only the test harness's console control connection, which
closed on Windows; it does not replace the MCP subprocess pipes under test.

Earlier Windows attempts failed in setup: an asar path-separator lookup error,
startup that never reached ready (followed by path handling corrections), then
closure of the harness's
console input. These are emulator portability issues and are not evidence of the
reported intermittent HA-MCP hang. An older unbounded startup attempt was left to
its workflow timeout rather than cancelled. The current preflight has a 120-second
watchdog and writes bootstrap diagnostics for future failures.

The shared harness was rechecked on Linux after the Windows portability changes:
[run 34158279894](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34158279894),
commit `3e5409ec`, passed the native large-message preflight and the SDK check
(tool/resource discovery, resource read, large prompt and eight concurrent large
tool calls). This is the final compatibility check, not another HAOS matrix run.
The older startup attempt 34157397313 ended at its configured 15-minute workflow
limit with GitHub's `cancelled` conclusion; no cancellation was requested.

## Later chat-session experiment

[SESSION-EMULATION.md](SESSION-EMULATION.md) describes the expanded harness and
its separate results. It uses the original chat preload and connection lifecycle
with a simulated signed-in page, and adds conversation changes and renderer
reloads. The original transport-only results above remain distinct.
