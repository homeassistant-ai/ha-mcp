# Issue 2367: isolated Desktop/HAOS investigation

No cause has been established and no product fix has been made. A passing batch
is a failed attempt to trigger the intermittent hang, not evidence the report is
resolved. Normal E2E lanes have not been migrated. This branch's manual-only
replacement for `haos-e2e-tests.yml` must not be merged into normal CI.

All package installation, source extraction, application execution, tests and
HAOS VM work happen on GitHub runners. The original shared HAOS cache is read
only; newly built pristine images are saved under an investigation-only key.

## Reports and scope

- [#2367](https://github.com/homeassistant-ai/ha-mcp/issues/2367): Windows Desktop
  launches a stdio bridge to an app/component HTTP endpoint. Repeated attempts
  with the identical transform can hang or complete immediately.
- [#1644, maintainer reproduction](https://github.com/homeassistant-ai/ha-mcp/issues/1644#issuecomment-4725519624):
  Windows Desktop with standalone local stdio hung twice on dashboard reads,
  followed by several hundred successful attempts. Diagnostics later failed to
  catch another occurrence. The same thread also contains app/bridge reports.

These observations require coverage of reads and writes, fresh and reused
processes, and request/response delivery. They do not establish the cause or
prove all reports have one cause. An idle `py-spy` thread stack alone cannot
exclude suspended asynchronous tasks.

## Experiments

`repro_issue_2367.py` is the earlier SDK-only embedded baseline; see
[RESULTS.md](RESULTS.md). `repro_desktop_issue_2367.py` uses extracted Desktop
transport code running inside native Electron/Chromium and real MessagePorts.
See [DESKTOP-VERIFICATION.md](DESKTOP-VERIFICATION.md) for source provenance,
Windows comparisons, runtime details and emulation limits.

The Desktop HAOS matrix has eight cases: two protocol versions, single/dual
channels, and default/false MandatoryBPS. Each uses three fresh Desktop processes
and thirty repeated edits in a reused process. Every edit starts with a dashboard
read through Desktop, then independent readback and restoration. Secondary
channels, when selected, continuously read the dashboard. The exact attached
large transform is alternated with a one-key static edit.

The original dashboard remains byte-identical to the
[attachment](https://github.com/user-attachments/files/31871296/dashboard-media-sanitized.yaml).
The first completed app/component runs used `mdi:music-box` for the small edit;
subsequent standalone attempts use the exact reported `mdi:music-box-multiple`.
The latter is already present in the supplied baseline, so successful no-change
writes must leave the hash unchanged. This difference is recorded explicitly.

Standalone mode launches `uvx --from <checkout wheel> ha-mcp` outside HA and removes
baked MCP integrations before boot. App/component modes launch
`uvx --from fastmcp-remote==4.0.3 fastmcp-remote <URL> --auth none` and use a real
HA-hosted server. Strict BPS acknowledgment is disabled for the comparison;
attaching skills to write responses remains controlled by MandatoryBPS.

A separate manual Windows job runs the Windows bundle's extracted transport on
stock Electron 42.10.0, with native Windows pipes and Chromium MessagePorts. Its
echo preflight does not use HAOS or an authenticated Desktop chat session.

## Limits

The HAOS runs use Linux Desktop transport, current checkout source and Core
2026.9.1, not the reporter's exact historical client/server installation. They
preserve the real dashboard, but do not recreate the 1811-entity household,
frontend custom-card rendering, authenticated chat orchestration, original
Desktop logging, or a long-lived human conversation. Windows source equivalence
and a Windows echo round trip cannot fill those gaps. Fast, instrumented batches
may miss timing or lifecycle conditions present in normal Desktop use.
