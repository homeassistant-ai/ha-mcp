# Regression Protocol

When a story's score decreases compared to its baseline, follow this protocol to confirm whether the regression is real and identify its cause.

## Overview

```
Decreased score detected
  |
  +- Step 1: Re-run (up to 3x)
  |   +- Still decreased? -> Continue
  |   +- Passes on retry? -> Flaky, note and move on
  |
  +- Step 2: Control run (baseline version)
  |   +- Baseline also fails? -> Not a regression, environment issue
  |   +- Baseline passes? -> Confirmed regression
  |
  +- Step 3: Cross-agent check
  |   +- Other agent also regressed? -> Code-level regression
  |   +- Other agent fine? -> Agent-specific issue
  |
  +- Step 4: Diagnose
  |   +- git diff between versions
  |   +- Identify likely cause
  |
  +- Step 5: Report
```

## Step 1: Re-Run (Flakiness Check)

Re-run the failed story up to 3 times. Agent behavior is non-deterministic, so a single failure may be a fluke.

```bash
# Re-run specific story
uv run python tests/uat/stories/run_story.py \
  catalog/s01_automation_sunset_lights.yaml \
  --agents gemini \
  --keep-container
```

**Decision:**
- Passes on any retry: Mark as `flaky` and move on. Note the pass/fail ratio.
- Fails all 3 times: Continue to Step 2.

## Step 2: Control Run (Baseline Version)

Run the same story against the baseline version to confirm the regression is real.

```bash
# Run against baseline version
uv run python tests/uat/stories/run_story.py \
  catalog/s01_automation_sunset_lights.yaml \
  --agents gemini \
  --branch v6.6.1 \
  --keep-container
```

**Decision:**
- Baseline also fails: Not a regression - likely environment issue, flaky test, or agent model change. Note and move on.
- Baseline passes, current fails: Confirmed regression. Continue to Step 3.

## Step 3: Cross-Agent Check

Run the same story with a different agent to determine if the regression is code-level or agent-specific.

```bash
# Run with different agent
uv run python tests/uat/stories/run_story.py \
  catalog/s01_automation_sunset_lights.yaml \
  --agents claude \
  --keep-container
```

**Decision:**
- Both agents regressed: Code-level regression (a tool changed behavior). Continue to Step 4.
- Only one agent regressed: Agent-specific issue (model update, prompt sensitivity). Note as agent-specific.

## Token-Based Regression Detection (Secondary)

Token cost is a **secondary metric** — it triggers investigation, not automatic failure.

- Extract billable tokens (non-cached input + output + thoughts) from session files
- Compare against baseline: >30% increase = investigate (check for KV-cache misses first, see SKILL.md Step 7)
- Common false positives: KV-cache misses (provider-side), agent exploring differently between runs

**Not used for regression detection:**
- Cached tokens — free and vary based on provider-side KV-cache behavior
- Wall-clock time — too noisy (network latency, cache misses, server load)

A story can pass functionally but show higher token cost. Investigate the cause before labeling it a regression.

## Step 4: Diagnose

Identify the likely cause of the regression.

### Git Diff Analysis

```bash
# Find the baseline SHA from JSONL
baseline_sha=$(grep '"story":"s01"' local/uat-results.jsonl | grep '"passed":true' | tail -1 | jq -r '.sha')

# Diff src/ between baseline and current
git diff ${baseline_sha}..HEAD -- src/
```

### Focus Areas

Look at changes in these files first:

| Changed File | Impact |
|-------------|--------|
| `src/ha_mcp/tools/tools_*.py` | Tool behavior changes |
| `src/ha_mcp/tools/smart_search.py` | Entity search behavior |
| `src/ha_mcp/tools/device_control.py` | Device control behavior |
| `src/ha_mcp/client/rest_client.py` | API interaction changes |
| `src/ha_mcp/errors.py` | Error message changes |

### Session Comparison

Compare the session files from the passing baseline run and failing current run:

```python
# Gemini session comparison
baseline_tools = extract_tool_calls(baseline_session)
current_tools = extract_tool_calls(current_session)

# Look for:
# - New error responses
# - Different tool call patterns
# - Changed return values
```

## Step 5: Report

Document the regression finding:

```markdown
### Regression Report: s01 (Sunset Porch Light)

**Status:** Confirmed regression
**Agent:** gemini
**Baseline:** v6.6.1 (sha: abc123) - PASS
**Current:** uat-stories (sha: def456) - FAIL

**Cause:** `ha_config_set_automation` now returns a different error format
when the automation alias contains spaces, breaking the agent's retry logic.

**Changed files:**
- `src/ha_mcp/tools/tools_config_automation.py` (lines 45-60)

**Suggested fix:** Update error message format to preserve backward compatibility.
```

## Flakiness Tracking

When a story is flaky (passes on retry), track it:

```jsonl
{"story":"s01","agent":"gemini","flaky":true,"attempts":3,"passed":2,"failed":1,"timestamp":"..."}
```

If a story is consistently flaky (>30% failure rate over multiple evaluation runs), consider:
1. Making the story prompt more explicit
2. Adding more setup steps
3. Increasing the agent timeout
4. Splitting into simpler sub-stories

## Decision Matrix

| Re-run Result | Control Result | Cross-Agent | Verdict |
|--------------|---------------|-------------|---------|
| Passes on retry | N/A | N/A | Flaky - note and move on |
| Fails 3x | Also fails | N/A | Environment issue |
| Fails 3x | Passes | Also regressed | Code regression - diagnose |
| Fails 3x | Passes | Only one agent | Agent-specific - note |
