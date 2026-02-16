---
name: bat-story-eval
description: Compare MCP tool behavior between target and baseline versions using pre-built and custom stories with diff-based triage.
disable-model-invocation: true
argument-hint: --baseline v6.6.1 [--agents gemini] [--stories s01,s02]
allowed-tools: Bash, Read, Write, Glob, Grep, Task
---

# BAT Story Evaluation

You are the evaluator. Follow these steps IN ORDER. Do not skip steps.

## Parse Arguments

From `$ARGUMENTS`, extract:
- `--baseline`: REQUIRED. Git tag/branch of the released version (e.g., `v6.6.1`).
- `--agents`: Agent list (default: `gemini`). Comma-separated.
- `--stories`: Force specific pre-built story IDs (e.g., `s01,s02`). Overrides triage selection.
- `--all-stories`: Skip triage, run ALL pre-built stories.
- `--keep-container`: Keep HA containers alive after run for manual inspection.
- `--model`: Model for Claude agent (e.g., `haiku`, `sonnet`).

If `$ARGUMENTS` is `--help` or missing `--baseline`, show usage and stop:
```
/bat-story-eval --baseline v6.6.1
/bat-story-eval --baseline v6.6.1 --agents gemini,claude
/bat-story-eval --baseline v6.6.1 --stories s01,s02
/bat-story-eval --baseline v6.6.1 --all-stories --agents claude --model haiku
```

## Step 0: Triage (Diff Analysis + Custom Story Design)

### 0a. Compute Diff

```bash
cd /home/julien/github/ha-mcp/worktree/uat-stories
git diff <baseline>..HEAD -- src/ha_mcp/ --stat
git diff <baseline>..HEAD -- src/ha_mcp/ --name-only
```

Classify changed files:
- **Tool modules** (`tools/tools_*.py`): specific tool implementations changed
- **Core code** (`client/`, `server.py`, `errors.py`, `tools/util_helpers.py`): affects all tools
- **Utilities** (`utils/`, `resources/`): may affect all tools
- **No src/ changes**: only tests/docs/config — select 2 smoke-test stories

### 0b. Select Pre-built Stories

Skip if `--stories` or `--all-stories` was passed.

1. Read the diff from 0a
2. Read all story YAMLs in `tests/uat/stories/catalog/s*.yaml` (title, description, prompt, setup)
3. For each story, reason about whether the diff could affect its outcome:
   - What tools/code paths would this story exercise?
   - Do any of those overlap with what changed?
4. Rules:
   - Story likely exercises changed code -> **selected**
   - Core code changed (`client/`, `server.py`, `errors.py`) -> **all stories selected**
   - No src/ changes -> 2 representative stories as smoke test
5. Report which stories were selected and why (one sentence per story)

### 0c. Design Custom Stories (at least 1)

Read the diff carefully. Your job is to catch regressions. For each changed code path NOT covered by selected pre-built stories, ask: "could this break something a user would notice?" If yes, design a custom story to test that hypothesis.

**Guidelines**: Always create at least 1 custom story. Each must test a distinct regression hypothesis — don't create stories that overlap. Stop when you've covered the risky gaps.

Write each as `/tmp/custom_c<NN>.yaml` using the standard story format:

```yaml
id: c01
title: "Short description of what is being tested"
category: custom
weight: 5
description: >
  Rationale: [what changed in the diff and why this scenario tests it]

setup:
  - tool: ha_config_set_helper
    args:
      helper_type: "input_boolean"
      name: "Test Entity Name"

prompt: >
  [Natural language request a real user would make that exercises the changed code]

teardown: []

verify:
  questions:
    - "Did the agent achieve the expected outcome?"
    - "Did it use the expected tools?"

expected:
  tools_should_use:
    - ha_search_entities
  description: >
    [What a correct agent should do]
```

**Design principles:**
- Focus on code paths that changed in the diff
- Plausible user scenarios, not synthetic edge cases
- Setup creates realistic HA state via FastMCP in-memory steps
- Prompts are what a real user would type
- At least 1. Each tests a distinct regression hypothesis. Stop when gaps are covered.

## Step 1: Run Baseline Version

For EACH agent, run all stories against the **baseline** version. One container per agent, reused across all stories.

### 1a. Start container with first story

```bash
cd /home/julien/github/ha-mcp/worktree/uat-stories
uv run python tests/uat/stories/run_story.py \
  catalog/<first_story>.yaml \
  --agents <agent> --keep-container \
  --branch <baseline> \
  --results-file local/uat-results.jsonl
```

**CAPTURE from stderr**: HA URL (e.g., `http://localhost:32771`), token, session file path.

### 1b. Verify, then run remaining pre-built stories

After each story, verify via ha_query.py using the story's `verify.questions`:
```bash
uv run python tests/uat/stories/scripts/ha_query.py \
  --ha-url http://localhost:PORT --ha-token TOKEN \
  --agent <agent> \
  "Does an automation with alias 'Sunset Porch Light' exist?"
```
Record each answer as **confirmed** / **denied** / **unclear**.

Run remaining pre-built stories on the same container:
```bash
uv run python tests/uat/stories/run_story.py \
  catalog/<next_story>.yaml \
  --agents <agent> --ha-url http://localhost:PORT --ha-token TOKEN \
  --branch <baseline> \
  --results-file local/uat-results.jsonl
```
Verify each immediately after running.

### 1c. Run custom stories on same container

```bash
uv run python tests/uat/stories/run_story.py \
  /tmp/custom_c01.yaml \
  --agents <agent> --ha-url http://localhost:PORT --ha-token TOKEN \
  --branch <baseline> \
  --results-file local/uat-results.jsonl
```
Verify each via ha_query.py using the custom story's `verify.questions`.

### 1d. Stop container

```bash
docker stop $(docker ps -q --filter "ancestor=ghcr.io/home-assistant/home-assistant:2026.1.3") 2>/dev/null
```

## Step 2: Run Target Version

Repeat Step 1 for the **target** (local code). Same stories, same order, fresh container.

The only difference: omit `--branch` so run_story.py uses local code.

```bash
uv run python tests/uat/stories/run_story.py \
  catalog/<first_story>.yaml \
  --agents <agent> --keep-container \
  --results-file local/uat-results.jsonl
```

Same container reuse for remaining stories (`--ha-url`). Same verification after each.

## Step 3: White-Box Analysis

For each story on each version, read the session file captured during the run.

**Gemini sessions** (JSON):
```bash
python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for msg in data.get('messages', []):
    for tc in msg.get('toolCalls', []):
        print(f\"  {tc['name']} ({tc.get('status', '?')})\")
" /path/to/session.json
```

**Claude sessions** (JSONL):
```bash
python3 -c "
import json, sys
for line in open(sys.argv[1]):
    entry = json.loads(line)
    if entry.get('type') == 'assistant':
        for b in entry.get('message', {}).get('content', []):
            if b.get('type') == 'tool_use':
                print(f\"  {b['name']}\")
" /path/to/session.jsonl
```

Compare against `expected.tools_should_use`:
- All expected tools used? (High weight)
- Tool failures with recovery? (Medium weight)
- Total tool call count (Low weight, note it)

## Step 4: Score & Compare

### Scoring Matrix

| Black-Box | White-Box | Score |
|-----------|-----------|-------|
| Entity correct + right structure | Right tools | **pass** |
| Entity correct + right structure | Wrong tools or recovered errors | **pass** (with notes) |
| Entity correct + wrong structure | Any | **partial** |
| Entity not created | Any | **fail** |

### Billable Tokens

Extract from session files:
```python
# Gemini: input includes cached, so subtract
billable = (input - cached) + output + thoughts

# Claude: input_tokens is already non-cached
billable = input + output
```

### Trend (target vs baseline)

For each story+agent:
- Both pass -> **stable**
- Target pass, baseline fail -> **improved**
- Target fail, baseline pass -> **decreased** (REGRESSION)
- Custom story, first run -> **new**
- Billable tokens >30% higher -> **cost regression** (even if pass)

## Step 5: Update JSONL

Append eval results as NEW lines (never modify existing):
```python
record["eval_score"] = "pass"  # or "partial" or "fail"
record["eval_notes"] = "Entity created, triggers verified"
record["eval_trend"] = "stable"  # or "new", "improved", "decreased"
```

## Step 6: Report

### Triage Summary

```
Diff: <baseline>..HEAD — N files changed in src/ha_mcp/
Selected pre-built: s01, s03, s05 (3 stories — tools_automation.py, tools_search.py changed)
Custom stories: c01, c02 (2 stories — covering error handling, fuzzy search threshold)
Skipped: s02, s04, s06-s12 (tools unchanged)
```

### Pre-built Story Results

```
| Story | Agent  | Baseline | Target | Trend  | Baseline Tokens | Target Tokens | Delta |
|-------|--------|----------|--------|--------|-----------------|---------------|-------|
| s01   | gemini | pass     | pass   | stable | 36,262          | 34,100        | -6%   |
| s03   | gemini | pass     | pass   | stable | 42,000          | 41,500        | -1%   |
```

### Custom Story Details

For EACH custom story, output a full section:

```
#### c01: [Title]

**Rationale**: [What changed in the diff and why this tests it]

**Setup**:
- Created input_boolean "Sophisticated Kitchen Sensor" via FastMCP

**Test prompt**: "[The exact prompt sent to the agent]"

**Verification**:
| Question | Baseline | Target |
|----------|----------|--------|
| Found the entity? | confirmed | confirmed |
| Used ha_search_entities? | confirmed | confirmed |

**Score**: baseline=pass, target=pass, trend=stable
**Tokens**: baseline=28,500, target=27,200 (-5%)
```

### Regressions

If any trend = `decreased`:
1. Flag prominently
2. Suggest re-run to check flakiness
3. Show relevant section of `git diff <baseline>..HEAD`

## Step 7: Investigate Outliers

When a story has >30% more billable tokens vs baseline, check for KV-cache misses:

```python
for i, msg in enumerate(data["messages"]):
    tok = msg.get("tokens", {})
    cached = tok.get("cached", 0)
    total = tok.get("input", 0)
    print(f"Turn {i+1}: input={total:,} cached={cached:,} non-cached={total-cached:,}")
```

A turn with `cached=0` after a non-cold-start turn = KV-cache miss (provider-side, not a code regression).

## Step 8: Tool Description Size

Compare tool description sizes between versions:

```bash
uv run python tests/uat/stories/scripts/measure_tools.py \
  --output local/tool-sizes-target.json
uv run python tests/uat/stories/scripts/measure_tools.py \
  --output local/tool-sizes-baseline.json --branch <baseline>
```

Flag >5% total size increase (directly impacts token cost per turn).

## Key Files

| File | Purpose |
|------|---------|
| `tests/uat/stories/run_story.py` | Story runner (container, setup, agent CLI) |
| `tests/uat/stories/scripts/ha_query.py` | Query live HA via agent+MCP for verification |
| `tests/uat/stories/catalog/s*.yaml` | Pre-built story definitions |
| `local/uat-results.jsonl` | Historical results (gitignored) |

## Important Notes

- `--baseline` is required: it's both the diff source and the control group
- Run pre-built stories BEFORE custom stories (cleanest state)
- ALWAYS verify each story via ha_query.py before running the next
- Reuse containers: first story starts it (`--keep-container`), rest use `--ha-url`
- Custom story YAMLs go to `/tmp/` (ephemeral); full details reported in Step 6
- Never use cached tokens for cost evaluation (they're free and variable)
- Compare **billable tokens**: non-cached input + output + thoughts
- The working directory MUST be `/home/julien/github/ha-mcp/worktree/uat-stories` for `uv run`
