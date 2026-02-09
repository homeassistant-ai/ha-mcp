---
name: bat
description: Run bot acceptance tests to validate MCP tools work correctly from a real AI agent's perspective. Use when testing PRs, detecting regressions, or verifying tool changes end-to-end with Claude/Gemini CLIs.
disable-model-invocation: true
argument-hint: [scenario-description or --help]
allowed-tools: Bash, Read, Write
---

# BAT - Bot Acceptance Testing

Bot acceptance testing validates that MCP tools work correctly from a real AI agent's perspective. You design test scenarios dynamically, run them via `tests/uat/run_uat.py`, and evaluate results.

## When to Use BAT

- **PR validation**: Test that tool changes work correctly from an agent's perspective
- **Regression detection**: Compare behavior between branches
- **Integration verification**: Ensure MCP tools work end-to-end with real agent CLIs

## Workflow

1. **Analyze the change**: Read the diff, identify which tools are affected
2. **Design scenario**: Generate a scenario JSON with setup/test/teardown prompts
3. **Run the script**: Pipe the scenario to `python tests/uat/run_uat.py`
4. **Evaluate summary**: Check `all_passed` per agent. If true, you're done.
5. **Dig deeper on failure**: Read `results_file` for full output, stderr, raw JSON
6. **Regression check**: If test fails, re-run with `--branch master` to compare

## Output Structure

The runner returns a **concise summary** to stdout (saves context when all passes):

```json
{
  "results_file": "/tmp/bat_results_abc123.json",
  "agents": {
    "gemini": {
      "all_passed": true,
      "test": {
        "completed": true,
        "duration_ms": 8100,
        "exit_code": 0,
        "num_turns": 5,
        "tool_stats": { "totalCalls": 4, "totalSuccess": 4, "totalFail": 0 }
      },
      "aggregate": {
        "total_duration_ms": 15300,
        "total_turns": 12,
        "total_tool_calls": 9,
        "total_tool_success": 9,
        "total_tool_fail": 0
      }
    }
  }
}
```

- **Phase stats**: `num_turns`, `tool_stats` (per phase) for fine-grained comparison
- **Aggregate stats**: Total counts across all phases for overall efficiency comparison
- **On failure**: also includes `output` and `stderr` for diagnosis
- **Full results**: raw JSON, complete output always available at `results_file`

## Scenario Design Guidelines

- **setup_prompt**: Create any entities/state the test needs
- **test_prompt**: Exercise the tools being tested, ask the agent to report results clearly
- **teardown_prompt**: Clean up created entities
- Keep prompts focused - each scenario tests ONE behavior
- Ask the agent to report: what succeeded, what failed, any unexpected behavior

## Example: Testing Error Signaling

```bash
cat <<'EOF' | python tests/uat/run_uat.py --agents gemini
{
  "setup_prompt": "Create a test automation called 'bat_error_test' with a time trigger at 23:59 and action to turn on light.bed_light.",
  "test_prompt": "Try to get automation 'automation.nonexistent_xyz'. Report if the tool signaled an error or returned a normal response. Then get automation 'automation.bat_error_test' and report its structure.",
  "teardown_prompt": "Delete automation 'bat_error_test' if it exists."
}
EOF
```

## Regression Comparison Workflow

**Full BAT comparison** (recommended):

1. **Pull latest master**: `git fetch origin master && git checkout master && git pull`
2. **Run on master**: Save scenario to file, run and save results
3. **Switch to branch**: `git checkout feat/my-branch`
4. **Run on branch**: Run same scenario, compare stats

**Compare these metrics:**
- **Task completion**: Did both pass? Any new failures?
- **Accuracy**: Check agent output quality - did it understand the task correctly?
- **Efficiency**: Compare `aggregate.total_tool_calls`, `aggregate.total_turns`, `aggregate.total_duration_ms`
- **Variation testing**: Ask the same task in different ways to test robustness

**Quick comparison** (single command):

```bash
# Test the PR branch
echo '{"test_prompt":"..."}' | python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini

# Compare against master
echo '{"test_prompt":"..."}' | python tests/uat/run_uat.py --branch master --agents gemini
```

## Cost Awareness

Each scenario invocation costs API credits (one per agent per phase). Design scenarios efficiently:

- Combine related checks in a single test_prompt when possible
- Only use setup/teardown when the test needs specific state
- Start with one agent, expand to both only when cross-agent comparison matters

## Handling Arguments

When `/bat` is invoked with arguments:

**If arguments contain a scenario description**, generate the JSON scenario and run it:
```
/bat test automation create with sunrise trigger then modify to sunset
```
→ Generate appropriate scenario JSON and execute

**If `--help` or no arguments**, show this help text.

**Otherwise**, treat `$ARGUMENTS` as instructions for what to test and design+run the scenario accordingly.

## Full Documentation

For complete CLI reference and output format, see `tests/uat/README.md`.
