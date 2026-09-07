# Issue 2367 HAOS reproduction

Manual, branch-only investigation. No product changes and no PR intended.
The branch version of `haos-e2e-tests.yml` reuses the embedded lane setup,
restores (never saves) the shared image, and boots one HAOS VM. The File & YAML
Tools entry is removed, retaining the embedded server entry. All test execution,
dependency installation, VM boot and image building happen on GitHub.

The attached YAML is preserved byte for byte from:
https://github.com/user-attachments/files/31871296/dashboard-media-sanitized.yaml

The explicit pytest file is `tests/src/e2e/haos_only/repro_issue_2367.py`;
its filename keeps it out of normal collection. Four cases cross direct HTTP /
fastmcp-remote 4.0.3 with omitted / false MandatoryBPS. Each runs five fresh
sessions then 30 writes in a reused session, alternating the reported exact
large transform and a one-key static icon change. Each write is restored and
checked against the baseline hash. A separate connection reads every ten seconds
if a write stalls. Calls allow the reported 240-second timeout. The first failure
stops the investigation with VM diagnostics and JSONL timings preserved.

Limits: Linux SDK clients, not Windows Claude Desktop or Claude Code. Existing
HAOS lane pins and current branch source, not the reporter's old Core/component
versions. Real attached dashboard shape, but not their 1811-entity installation
or frontend custom-card rendering. Default BPS is explicitly compared to false;
the lane disables strict attestation gating, as in the reporter's latest setup.
Backups retain the embedded server default (enabled).

Dispatch only this investigation branch; do not merge this workflow replacement.
