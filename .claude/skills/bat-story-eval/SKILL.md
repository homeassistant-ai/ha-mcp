---
name: bat-story-eval
description: Run UAT stories, verify results against live HA, score pass/partial/fail, detect regressions.
disable-model-invocation: true
argument-hint: [--agents gemini] [--stories s01,s02] [--branch v6.6.1]
allowed-tools: Bash, Read, Write, Glob, Grep, Task
---

# BAT Story Evaluation

You are the evaluator. Follow these steps IN ORDER. Do not skip steps.

## Parse Arguments

From `$ARGUMENTS`, extract:
- `--agents`: Agent list (default: `gemini`). Comma-separated.
- `--stories`: Story IDs (default: all). Comma-separated, e.g. `s01,s02`.
- `--branch`: Git branch/tag (default: local code).
- `--keep-container`: Keep HA container alive after run.

If `$ARGUMENTS` is `--help` or empty, show usage and stop:
```
/bat-story-eval --agents gemini --stories s01
/bat-story-eval --agents gemini,claude --stories s01,s02 --branch v6.6.1
```

## Steps 1-2: Run and Verify Stories (ONE AT A TIME)

**CRITICAL: Run each story individually, verify it, then move on.**
Stories share a container per agent, so later stories can clobber earlier ones.
You MUST verify each story before running the next.

For EACH story in the list, repeat this loop:

### 1a. Run ONE story

```bash
cd /home/julien/github/ha-mcp/worktree/uat-stories
uv run python tests/uat/stories/run_story.py \
  catalog/s01_automation_sunset_lights.yaml \
  --agents gemini --keep-container \
  --results-file local/uat-results.jsonl
```

**CAPTURE from stderr output:**
- The HA container URL (e.g., `http://localhost:32771`)
- The HA token
- Session file path

### 1b. Black-Box Verify THIS story immediately

Read the story YAML to get `verify.questions`, then ask each one via ha_query.py:

```bash
uv run python tests/uat/stories/scripts/ha_query.py \
  --ha-url http://localhost:PORT --ha-token TOKEN \
  --agent gemini \
  "Does an automation with alias 'Sunset Porch Light' exist? Show its entity_id."
```

Record each answer as: **confirmed** / **denied** / **unclear**.

If ALL critical questions are confirmed -> black-box = PASS.
If entity doesn't exist -> black-box = FAIL.
If entity exists but wrong structure -> black-box = PARTIAL.

### 1c. Stop the container before the next story

```bash
docker stop $(docker ps -q --filter "ancestor=ghcr.io/home-assistant/home-assistant:2026.1.3") 2>/dev/null
```

Then repeat 1a-1c for the next story. Each story gets a fresh container.

## Step 3: White-Box Analysis (REQUIRED)

For EACH story that ran, read the session file captured in Step 1.

**Gemini sessions** (JSON file):
```bash
cat /path/to/session-*.json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for msg in data.get('messages', []):
    for tc in msg.get('toolCalls', []):
        status = tc.get('status', '?')
        print(f\"  {tc['name']} ({status})\")
"
```

**Claude sessions** (JSONL file):
```bash
grep '"tool_use"' /path/to/session.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    entry = json.loads(line)
    for block in entry.get('message', {}).get('content', []):
        if block.get('type') == 'tool_use':
            print(f\"  {block['name']}\")
"
```

Then read the story YAML `expected.tools_should_use` and compare:
- Were all expected tools used? (High weight)
- Were there tool failures followed by recovery? (Medium weight)
- How many total tool calls? (Low weight, just note it)

## Step 4: Score Each Story

Combine black-box and white-box into a final score:

| Black-Box | White-Box | Score |
|-----------|-----------|-------|
| Entity correct + right structure | Right tools used | **pass** |
| Entity correct + right structure | Wrong tools or recovered errors | **pass** (with notes) |
| Entity correct + wrong structure | Any | **partial** |
| Entity not created | Any | **fail** |

## Step 5: Compare Against Baseline (REQUIRED)

Read the JSONL results file and find the MOST RECENT passing result for the same story+agent:

```bash
grep '"story":"s01"' local/uat-results.jsonl | grep '"agent":"gemini"' | grep '"passed":true' | tail -1
```

Compare:
- If no baseline exists: trend = `new`
- If current passed and baseline passed: trend = `stable`
- If current passed but baseline failed: trend = `improved`
- If current failed but baseline passed: trend = `decreased` (REGRESSION)

## Step 6: Update JSONL with Eval Results

For EACH scored story, append a NEW line to the JSONL (do NOT modify existing lines).
Read the LAST line for this story+agent to get the raw data, then write a new line with eval fields added:

```python
import json
# Read the last result for this story+agent
# Add these fields:
record["eval_score"] = "pass"  # or "partial" or "fail"
record["eval_notes"] = "Entity created correctly, sun triggers verified, correct tools used"
record["eval_trend"] = "stable"  # or "new", "improved", "decreased"
# Write as new JSONL line
```

## Step 7: Report

Output a summary table to the user:

```
| Story | Agent  | Score   | Trend   | Notes |
|-------|--------|---------|---------|-------|
| s01   | gemini | pass    | stable  | Sun triggers verified |
| s02   | gemini | pass    | new     | First run, no baseline |
```

If any story has trend = `decreased`, flag it prominently and suggest:
1. Re-run to check for flakiness
2. Run against baseline branch as control
3. Check `git diff` between baseline SHA and current

## Key Files

| File | Purpose |
|------|---------|
| `tests/uat/stories/run_story.py` | Story runner (starts container, runs agents) |
| `tests/uat/stories/scripts/ha_query.py` | Query live HA via agent+MCP |
| `tests/uat/stories/catalog/s*.yaml` | Story definitions with `verify` + `expected` |
| `local/uat-results.jsonl` | Historical results (gitignored) |
| `references/evaluation-protocol.md` | Detailed scoring criteria (read if unsure) |
| `references/regression-protocol.md` | Full regression investigation protocol |

## Important Notes

- Run ONE story at a time: run -> verify -> stop container -> next story
- ALWAYS use `--keep-container` so you can run ha_query.py after each story
- Stop the container after verifying, before running the next story
- The working directory MUST be the worktree root for `uv run` to work
- ha_query.py needs the container URL and token from stderr output
- Do NOT skip the black-box verification - it's the ground truth
- Session files may be large; extract just tool calls, don't read entire files
