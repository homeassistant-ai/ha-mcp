# Transport recovery experiment for issue 2367

Separate investigation branch based on master 3439bbb1, without PR #2403's
changes. Other documents here describe the earlier reused Desktop harness.

Hypothesis: a connection interruption leaves Desktop or fastmcp-remote in a state
where later dashboard writes time out while reads still work.

The new branch-scoped workflow starts disposable GitHub HAOS VMs for embedded
(no optional tools entry) and app/add-on. Existing workflows, shared fixtures,
HAOS helpers, image builders, and production code remain unchanged.

Pinned official Linux Desktop executes extracted real stdio transport,
connection lifecycle, and chat preload with a local simulated signed-in page.
Setup compares selected transport code against pinned official Windows Desktop.
This does not reproduce an authenticated cloud chat or Windows OS behavior.
The bridge is fastmcp-remote==4.0.3 with --auth none.

After a successful control write on the reporter's exact dashboard, inject:

1. Half an advertised HTTP request body, followed by an upstream disconnect.
2. A complete upstream write followed by a truncated response and disconnect.
3. One temporary 502 on a write, then normal forwarding.
4. A real homeassistant.restart through Desktop; require Core down/up and a
   fresh independent MCP read before recovery probes.

First three cases repeat three times, each with four post-fault writes.
Restart runs once with four post-restart writes. Both the exact large transform
and one-key icon edit are used. Keep Desktop and its bridge alive throughout
each case; traces reveal automatic reconnects.

Each recovery write gets same-Desktop and independent MCP read probes, during
its pending interval if it takes over two seconds. Writes retain a 240-second
deadline. Read back independently before any explicit hash-checked restoration;
never blindly retry an unknown write.

Interrupted calls may fail. Failed post-recovery writes fail the test case.
Only post-recovery write timeouts with healthy reads match the distinctive issue
signature. Ordinary 502s, global disconnections, and failed boots are separate.

Retain JSONL timings and proxy boundaries, Desktop IPC/pipe traces, JUnit,
HA diagnostics, package versions, source SHA, and fixture/transform checksums.
No local execution or live-home access is involved.

The supplied fixture already has the small edit target icon. That exact call is
a valid no-op; verify its resulting value and unchanged hash. The large edits
must change the hash and append the expected section. The first attempt stopped
on an incorrect unconditional changed-hash assertion, corrected before rerun.
