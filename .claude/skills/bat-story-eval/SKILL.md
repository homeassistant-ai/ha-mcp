---
name: bat-story-eval
description: Evaluate UAT stories with AI-driven black-box and white-box analysis. Runs stories, verifies results against live HA, analyzes session files, scores outcomes, and detects regressions.
disable-model-invocation: true
argument-hint: [--agents gemini] [--branch v6.6.1] [--stories s01,s02]
allowed-tools: Bash, Read, Write, Glob, Grep, Task
---

# UAT Evaluation Skill

Structured evaluation of UAT stories with regression detection. Unlike `/bat-adhoc` (ad-hoc testing), `/bat-story-eval` runs the full story catalog, scores results via black-box and white-box analysis, and compares against baseline.

## Architecture

```
/bat-story-eval [--agents gemini] [--branch v6.6.1] [--stories s01,s02]
  |
  +- For each agent:
  |   +- Start HA container (via run_story.py)
  |   +- For each story:
  |   |   +- Setup (FastMCP in-memory)
  |   |   +- Run test agent (gemini/claude CLI)
  |   |   +- Capture: session file path
  |   |
  |   +- EVALUATE each story (black box + white box)
  |   |   +- Black box: run ha_query.py against live container
  |   |   |   with verify.questions from story YAML
  |   |   +- White box: read session file for tool calls, thoughts, errors
  |   |   +- Score: pass/partial/fail + explanation
  |   |
  |   +- Compare against baseline (from JSONL)
  |   |   +- improved / stable / decreased
  |   |
  |   +- Stop container (or keep alive if --debug)
  |
  +- Regression protocol (if decreased):
  |   +- Re-run up to 3x (test + control against baseline version)
  |   +- Cross-check with other agent if still decreased
  |   +- If confirmed: git diff analysis between versions
  |
  +- Report + append scored results to JSONL
```

## Workflow

### Step 1: Parse Arguments

Parse `$ARGUMENTS` for:
- `--agents`: Comma-separated agent list (default: `gemini`)
- `--branch`: Git branch/tag to test (default: local code)
- `--stories`: Comma-separated story IDs to run (default: all)
- `--debug`: Keep containers alive for manual inspection
- `--baseline`: Branch/tag to compare against (default: latest JSONL baseline)
- `--help`: Show this documentation

### Step 2: Run Stories

Use `run_story.py` with `--keep-container` to run stories and keep the HA container alive for verification:

```bash
uv run python tests/uat/stories/run_story.py --all \
  --agents gemini \
  --keep-container \
  --results-file local/uat-results.jsonl
```

Capture from stderr:
- Container URL and token (for ha_query.py)
- Session file paths (for white-box analysis)

### Step 3: Black-Box Evaluation

For each story, use `ha_query.py` to ask the `verify.questions` from the story YAML against the live container:

```bash
uv run python tests/uat/stories/scripts/ha_query.py \
  --ha-url http://localhost:PORT --ha-token TOKEN \
  --agent gemini \
  "Does automation.sunset_porch_light exist? Show its triggers and actions."
```

Score each question as answered/unanswered. The evaluator agent should use a **different** agent than the test agent when possible (e.g., use gemini to evaluate claude's work).

### Step 4: White-Box Evaluation

Read the session file captured during the test run:

**Gemini sessions** (`~/.gemini/tmp/<hash>/chats/session-*.json`):
```python
# Read and parse
import json
session = json.loads(Path(session_file).read_text())
for msg in session["messages"]:
    if msg.get("toolCalls"):
        for tc in msg["toolCalls"]:
            print(f"  {tc['name']}({tc.get('status', 'unknown')})")
```

**Claude sessions** (`~/.claude/projects/<dir>/<session>.jsonl`):
```python
# Read JSONL line by line
for line in Path(session_file).read_text().splitlines():
    entry = json.loads(line)
    if entry.get("type") == "assistant":
        for block in entry["message"].get("content", []):
            if block.get("type") == "tool_use":
                print(f"  {block['name']}")
```

Evaluate against criteria in `references/evaluation-protocol.md`.

### Step 5: Score

For each story, assign a score:

| Score | Meaning |
|-------|---------|
| **pass** | Entity created correctly, right tools used, efficient execution |
| **partial** | Entity created but with issues (wrong triggers, extra steps, etc.) |
| **fail** | Entity not created, wrong entity, or critical errors |

### Step 6: Compare Against Baseline

Read the JSONL results file and find the most recent baseline for comparison:

```bash
# Find baseline results for comparison
grep '"story":"s01"' local/uat-results.jsonl | tail -5
```

Determine trend: `improved`, `stable`, or `decreased`.

### Step 7: Regression Protocol

If any story shows a `decreased` trend, follow the protocol in `references/regression-protocol.md`.

### Step 8: Report

Output a summary table:

```
| Story | Agent  | Score   | Trend     | Notes |
|-------|--------|---------|-----------|-------|
| s01   | gemini | pass    | stable    | -     |
| s02   | gemini | partial | decreased | Missing delay in automation |
| s03   | gemini | pass    | improved  | Now checks traces correctly |
```

Append scored results to the JSONL file with additional fields:
- `eval_score`: pass/partial/fail
- `eval_notes`: explanation
- `eval_trend`: improved/stable/decreased

## Key Files

| File | Purpose |
|------|---------|
| `tests/uat/stories/run_story.py` | Story runner with container-per-agent |
| `tests/uat/stories/scripts/ha_query.py` | Query HA via agent+MCP |
| `tests/uat/stories/catalog/s*.yaml` | Story definitions with `verify` sections |
| `local/uat-results.jsonl` | Historical results (gitignored) |
| `references/evaluation-protocol.md` | Black/white box scoring criteria |
| `references/regression-protocol.md` | Re-run, cross-check, diff procedures |

## Cost Awareness

Each evaluation run costs API credits. Minimize by:
- Running only specific stories (`--stories s01,s02`) during development
- Using one agent at a time (default: gemini only)
- Skipping regression protocol for known-flaky stories

## Handling Arguments

When `/bat-story-eval` is invoked:

**With `--help` or no arguments**: Show this documentation.

**With arguments**: Parse flags and execute the full evaluation workflow.

**Examples**:
```
/bat-story-eval --agents gemini --stories s01
/bat-story-eval --agents gemini,claude --branch v6.6.1
/bat-story-eval --agents gemini --debug
/bat-story-eval --baseline v6.5.0
```
