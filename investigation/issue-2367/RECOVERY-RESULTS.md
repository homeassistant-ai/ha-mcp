# Issue 2367 transport recovery results

Completed entirely on GitHub runners on 2026-09-08 UTC.

- Passing run: https://github.com/homeassistant-ai/ha-mcp/actions/runs/34176660906
- Tested commit: `9fced9251d20eca0b537c261d75587b3c9ef789e`
- Base: master `3439bbb1a1f664d0b6b9b314b9c3f223b0799e64`; no PR #2403 changes.
- Both HAOS jobs passed all four test cases, with zero skips or errors.
- Production code, existing workflow definitions, shared E2E fixtures, HAOS
  runtime helpers, and image builders were not modified by this experiment.

## Observations

| Backend | Post-recovery calls | Same-Desktop reads | Independent reads | Slowest post-recovery call |
| --- | ---: | ---: | ---: | ---: |
| Embedded, no optional tools entry | 40/40 passed | 40/40 passed | 40/40 passed | 107.43 ms |
| App/add-on | 40/40 passed | 40/40 passed | 40/40 passed | 81.06 ms |

Per backend, three upload interruptions, three response interruptions, three
502 injections, and one real Core restart were followed by recovery probes.
Four additional control writes per backend passed before fault injection.

Half of the 80 post-recovery calls were the reporter's large transform and
changed the hash as expected. The other half were the exact one-key icon call;
the supplied dashboard already has that value, so they correctly preserved the
hash. All calls used MandatoryBPS=false. Backups remained enabled.

The app's runtime log contains three exact occurrences of the reported stack:
`mcp.server.streamable_http._handle_post_request` -> `request.body()` ->
`starlette.requests.ClientDisconnect`, followed by the stateless-session crash
warning. These correspond to the deliberately truncated uploads. None poisoned
later requests or produced the write-timeout/read-success pattern.

- All truncated uploads left the dashboard unchanged.
- All injected 502s left the dashboard unchanged.
- All truncated responses occurred after the full write applied. The independent
  observer detected this before explicit restoration; no blind retry was used.
- The initiating restart call returned an error in both installations after
  about 21 seconds. The app logged a 502 from the Supervisor Core service proxy.
- Core down/up and successful fresh MCP reads were observed. Measured from
  restart dispatch to verified readiness: app 35.85 seconds, embedded 52.29
  seconds. These include polling and MCP readiness, not just Core startup time.
- All subsequent reads and writes worked without manual intervention.
- Desktop traces contain exactly one `opened` bridge process per test case,
  without renderer reloads or bridge relaunches. All eight Desktop instances
  exited cleanly after testing.
- No post-recovery write was still pending at the two-second read-probe point;
  no four-minute timeout occurred.

## Scope and conclusion

The actual Linux Desktop executable ran extracted transport, lifecycle, and
preload code with a simulated signed-in page. Selected transport definitions
again matched the pinned Windows bundle under identifier normalization.
The authenticated cloud chat frontend and native Windows OS behavior were not
exercised. Versions: Linux Desktop 1.46388.2, compared Windows 1.46388.4,
fastmcp-remote 4.0.3 with --auth none.

Reporter dashboard SHA256:
`2ccc763882e7abe4dd74b4a55bbc293db23e7a647f20696f0e2b23106c160329`

Original transform SHA256:
`dc186d196fadeec7dc580e1faa18a1b31cf3e2b532042e06f70481553488d67c`

This reproduces the generic body-disconnect exception and an error response
during planned restart, but not the persistent recovery failure proposed by the
hypothesis or the original intermittent Desktop hang. It does not establish a
shared cause with the new restart report and does not justify closing #2367.

Artifacts on the run contain JSONL measurements, JUnit, Desktop IPC/pipe traces,
source comparisons, package versions, and redacted Core/Supervisor/app logs.

## Initial harness correction

Run https://github.com/homeassistant-ai/ha-mcp/actions/runs/34175936591 stopped
on an incorrect assertion that every successful edit must change the hash.
The reporter fixture already has the small-edit icon value. Corrected that
assertion, retained exact payloads, and explicitly disabled pytest's default
three-failure cutoff so every case would run. Also preserved response encoding
headers in the fault proxy. The passing run above includes those corrections.
