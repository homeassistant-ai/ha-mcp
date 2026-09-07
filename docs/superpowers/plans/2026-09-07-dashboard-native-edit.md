# Native Dashboard Edits Implementation Plan

> Execute with subagent-driven-development for independent modules and review; all test execution goes to GitHub runners.

**Goal:** Component-backed dashboard writes and structured edits, preserving existing functionality.
**Architecture:** Shared pure patch semantics, a capability-negotiated Core handler,
server routing with conservative write outcomes, and unchanged legacy behavior.
**Tech Stack:** Python, FastMCP, Home Assistant Lovelace, existing GitHub Actions.
**Spec:** ../specs/2026-09-07-dashboard-native-edit.md

## Global Constraints

No local project execution, tests, imports, builds or installs. No existing workflow,
runner or E2E fixture changes. No PR until branch validation. No merge. No #2367 fix claim.

## Tasks

- [ ] Pure evaluator: `src/ha_mcp/utils/dashboard_patch.py` and identical component
  `dashboard_patch.py`; `apply_dashboard_patch(config, patch) -> dict` or ValueError.
  Tests exercise output and unchanged input on failed later operations, pointer
  escapes, array append/index validity, null/literal strings, no-op and limits.
- [ ] Component: `dashboard_edit.py` exposing async `async_edit_dashboard(hass,msg)`;
  WS producer wrapper/schema and `dashboard_edit` capability. Test using Core-like
  live dict/cached save lifecycle; require unchanged config/events on rejected writes.
- [ ] Server: `tools/component_dashboard_edit.py` for capability/error routing;
  `tools_config_dashboards.py` patch parameter and native save/verification. Keep
  legacy fallback and Python code in existing sandbox. Tests check observable
  results, call counts, concurrent conflict and no duplicate write on lost response.
- [ ] Validation: add cross-topology E2E cases alongside existing dashboard suites;
  run existing unit/container/HAOS workflows unchanged using workflow_dispatch.
  Compare reporter-fixture results and request volume. Run formatting remotely.
- [ ] Review: source-backed Core semantics, functional parity, errors and schema;
  confirm workflow/runner diff empty and clean current-head results, open draft PR,
  inspect normal CI and automated review and address verified findings.

## Evidence and decisions

Base master: 3439bbb1a1f664d0b6b9b314b9c3f223b0799e64.
Core dashboard.py/websocket.py/storage.py compared at 2026.8.3 and 2026.9.1;
identical sources. Store catches some persistence errors, so no fsync/durable-commit claim.
Explicit user approval in the conversation covers this implementation and post-proof PR.
