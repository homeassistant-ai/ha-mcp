# Evaluation Protocol

Scoring criteria for UAT story evaluation. Each story is evaluated on two dimensions: black-box (did it work?) and white-box (how did it work?).

## Black-Box Scoring (from ha_query.py)

Black-box evaluation uses `ha_query.py` to query the live HA instance after the test agent has run. The evaluator asks the `verify.questions` from the story YAML.

| Criterion | Weight | How to Check |
|-----------|--------|--------------|
| Entity created | Critical | Does the expected entity exist in HA? |
| Correct structure | High | Right triggers, conditions, actions, sequences? |
| Correct behavior | Medium | Would it actually work as intended? (right entity IDs, services, etc.) |
| No side effects | Low | Were unexpected entities created or modified? |

**Scoring rules:**
- If the critical criterion fails (entity not created), score is `fail` regardless of other criteria.
- If high-weight criteria fail (wrong structure), score is `partial`.
- If only medium/low criteria have issues, score is `pass` with notes.

### Asking Verification Questions

Use `ha_query.py` to ask each question from `verify.questions`. The response from the agent should be evaluated for accuracy:

```bash
uv run python tests/uat/stories/scripts/ha_query.py \
  --ha-url $HA_URL --ha-token $HA_TOKEN \
  --agent gemini \
  "Does an automation with alias 'Sunset Porch Light' exist? Show its triggers and actions."
```

Evaluate the response:
- **Confirmed**: The answer clearly confirms the expected outcome
- **Denied**: The answer clearly shows the expected outcome did NOT happen
- **Unclear**: The answer is ambiguous or incomplete

### Cross-Agent Verification

When possible, use a different agent for verification than the one that ran the test. This avoids bias (e.g., the same model might interpret ambiguous results favorably).

| Test Agent | Eval Agent (preferred) |
|------------|----------------------|
| gemini | gemini (same is fine for verification queries) |
| claude | gemini |

## White-Box Scoring (from session file)

White-box evaluation reads the agent's session file to understand HOW it accomplished the task.

| Criterion | Weight | What to Check |
|-----------|--------|---------------|
| Tool selection | High | Did the agent use the expected tools from `tools_should_use`? |
| Error recovery | Medium | Did the agent recover from tool failures gracefully? |
| Efficiency | Low | Tool call count, turns, unnecessary retries |
| Reasoning | Info | Did the agent's thoughts show understanding of the task? |

### Tool Selection Analysis

Compare the tools actually used against `expected.tools_should_use` from the story YAML:

```python
# Extract tool calls from Gemini session
tools_used = set()
for msg in session["messages"]:
    for tc in msg.get("toolCalls", []):
        tools_used.add(tc["name"])

expected_tools = set(story["expected"]["tools_should_use"])
missing = expected_tools - tools_used
unexpected = tools_used - expected_tools  # Not necessarily bad, just notable
```

**Scoring:**
- All expected tools used: Good
- Missing critical tools (e.g., `ha_config_set_automation` for an automation story): `fail`
- Extra tools used: Note but don't penalize (agent may have explored first)

### Error Recovery

Look for patterns in the session file:
- Tool calls with `status: "error"` followed by a retry or alternative approach: Good recovery
- Tool calls with `status: "error"` with no recovery: Penalize
- Repeated identical failing calls: Penalize

### Efficiency Metrics (Secondary)

These are **secondary metrics** — report them but don't use them to decide pass/fail:

- **Billable tokens** (non-cached input + output + thoughts) — directional cost signal. Flag >30% increase for investigation, but check for KV-cache misses before concluding regression.
- **Tool call count** — compare against baseline, but expect variation between runs due to agent exploration.
- **Total turns** — fewer is generally better, but not conclusive on its own.

**Not used for evaluation:**
- **Cached tokens** — free and vary based on provider-side KV-cache behavior.
- **Wall-clock time** — too noisy for meaningful comparison (network latency, KV-cache misses, server load).

## Scoring Matrix

| Black-Box | White-Box | Final Score |
|-----------|-----------|-------------|
| Entity correct + right structure | Right tools + no errors | **pass** |
| Entity correct + right structure | Wrong tools or errors recovered | **pass** (with notes) |
| Entity correct + wrong structure | Right tools | **partial** |
| Entity correct + wrong structure | Wrong tools | **partial** |
| Entity not created | Any | **fail** |
| Any | Critical tool missing | **fail** |

## Story-Specific Considerations

### Troubleshooting Stories (s03, s11)
These stories don't create entities - they analyze existing ones. For these:
- Black-box: Check if the agent identified the correct issue
- White-box: Check if the right diagnostic tools were used

### Read-Only Stories (s05, s11)
No entities to verify. Evaluate based on:
- Did the agent provide accurate information?
- Did it use the right tools to gather data?

### Update Stories (s07)
Special care needed:
- Verify the original entity still exists (wasn't deleted and recreated)
- Verify the modification was applied correctly
- Verify unchanged fields are preserved
