# HAOS reproduction result — 2026-09-07

The intermittent hang did not reproduce in this run. This does not establish
that the issue is fixed or that Claude Desktop is the cause.

- Run: https://github.com/homeassistant-ai/ha-mcp/actions/runs/34147904546
- Tested commit: `ed3832b89860014ad5b1eac2670598a5382e17eb`
- Base: `3439bbb1` from current `origin/master`.
- GitHub runner: Ubuntu 22.04, full HAOS VM, embedded server, no File & YAML Tools entry.
- Core reported at runtime: `2026.9.1`; HAOS builder pin: `18.2`.
- The fixture delivered the checkout-built `ha_mcp-8.4.3` wheel into the VM.
  Its metadata version is **not** proof of release-tag source: use the commit above.
- Driver: FastMCP 3.4.7 / MCP 1.28.1.
- Isolated bridge: fastmcp-remote 4.0.3 / MCP 2.2.0, `--auth none`.
- Pytest: **4 passed, 0 failed, 0 skipped**, 144.65 seconds including fixture setup.

| Transport | MandatoryBPS | Verified edits | Median edit | Slowest edit |
| --- | --- | ---: | ---: | ---: |
| Direct HTTP | Omitted/default | 35 | 77 ms | 100 ms |
| fastmcp-remote stdio bridge | Omitted/default | 35 | 75 ms | 136 ms |
| Direct HTTP | false | 35 | 55 ms | 63 ms |
| fastmcp-remote stdio bridge | false | 35 | 74 ms | 84 ms |

Each case used five fresh client sessions/processes, then 30 edits in one reused
session. Across all cases: 80 executions of the reported large transform and
60 one-key edits. Every edit changed the hash and expected dashboard content;
every restoration recovered the original configuration and hash.

The measurements contain 712 dispatched tool calls and 712 responses, including
140 target edits, 140 restoration writes, and four initial dashboard writes.
All 284 `ha_config_set_dashboard` calls completed; their slowest was 542 ms
(including setup). No exception or heartbeat event occurred: none of the attempts
lasted the ten seconds required to start the stalled-call read probe.

With default MandatoryBPS, serialized responses measured 31,256–32,166 bytes,
including 30,459 bytes of serialized skill content. With false, responses measured
656–1,566 bytes and omitted skill content. The larger response path was exercised
successfully, including through the stdio bridge.

Artifacts on the run contain `events.jsonl`, JUnit, dependency inventories,
tested SHA, VM logs, and redacted config-entry diagnostics. A local read-only
copy of the downloaded evidence is retained under
`ha-mcp/local/issue-2367-run-34147904546/`.

## Interpretation and limits

The supplied dashboard and transform do not reliably trigger the issue on the
current HAOS embedded server through these Linux SDK/bridge clients. Windows
Claude Desktop, its model-generated tool dispatch, the reporter's older Core
2026.8.3, their exact bridge version, and their 1811-entity installation were not
recreated. The custom cards were stored as dashboard configuration; this did not
run a browser or reproduce frontend integrations and devices.

The dashboard tool, Python sandbox, and embedded webhook files are unchanged
between tag v8.4.3 and the tested branch. Backup internals and dependency locks
have changed. Backups retain the embedded server's enabled default; strict BPS
attestation is disabled by the lane, matching the reporter's latest setting.

Source observation: `log_tool_usage` writes its record in `finally`, after the
wrapped tool exits, and the backup decorator runs outside it. Therefore absence
from that usage log alone cannot prove a call never reached dispatch. Separately,
the embedded webhook reads the complete body before forwarding it to MCP.
Neither observation identifies the cause of the Desktop failures.

No product fix, issue comment, PR, normal-CI change, or local test execution was
made. The workflow replacement exists only on the investigation branch and must
not be merged into normal CI.
