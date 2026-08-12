# Null Automation Trace State Fix Plan

**Goal:** Make `ha_get_automation_traces` return detailed time-driven template
traces instead of failing when Home Assistant records null trigger states.

**Architecture:** Preserve the formatter's current output contract and make the
two state extractions null-safe at the boundary where Home Assistant trace data
is projected into the MCP response. Cover the exact payload at unit level and
the natural Home Assistant trigger path at E2E level.

**Tech stack:** Python 3.13, pytest, FastMCP E2E client, Home Assistant
testcontainer in CI.

---

### Task 1: Add the focused unit regression

**Files:**
- Modify: `tests/src/unit/test_tools_traces_detail.py`

1. Add a trace whose template trigger has present-but-null `from_state` and
   `to_state` values.
2. Assert detailed formatting succeeds and retains both keys as `None`.
3. Run only that test and confirm it fails with the existing `AttributeError`.

### Task 2: Implement the minimal formatter fix

**Files:**
- Modify: `src/ha_mcp/tools/tools_traces.py`

1. Normalize each nullable state object before reading its `state` field.
2. Re-run the new regression and the targeted trace unit file.

### Task 3: Add the Home Assistant E2E regression

**Files:**
- Modify: `tests/src/e2e/workflows/automation/test_traces.py`

1. Create a cleanup-tracked automation with a time-dependent template trigger
   and a zero-delay action so no live or simulated device is touched.
2. Wait for its natural trace, fetch the detailed trace, and assert successful
   null state projection.
3. Do not run E2E locally; rely on the repository's Linux E2E CI lane.

### Task 4: Verify and publish

1. Run focused unit coverage, the `uv.lock`-pinned Ruff formatter/checker, and
   mypy locally.
2. Inspect the exact diff, commit on the isolated branch, push once to
   `upstream`, and open a draft PR using the repository template.
3. Monitor every CI check and the full CodeRabbit review body plus inline
   threads. Fix verified findings, reply, resolve threads, and repeat until the
   current head is green with no unresolved review findings.
