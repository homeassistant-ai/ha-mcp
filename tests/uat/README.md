# UAT Framework - Agent-Driven Acceptance Testing

Executes MCP test scenarios on real AI agent CLIs (Claude, Gemini) against a Home Assistant test instance. Designed to be driven by a calling agent that generates scenarios dynamically, runs them, and evaluates results.

## Architecture

```
Calling Agent (Claude Code)          run_uat.py              Agent CLIs
  |                                    |                        |
  |-- generates scenario JSON -------->|                        |
  |                                    |-- starts HA container  |
  |                                    |-- writes MCP configs   |
  |                                    |-- runs agents -------->|
  |                                    |                        |-- uses MCP tools
  |                                    |<-- collects output ----|
  |<-- returns results JSON -----------|                        |
  |                                                             |
  |-- evaluates pass/fail                                       |
```

- **No pre-built scenarios** - The calling agent generates them based on what it's testing
- **The runner is a dumb executor** - Takes JSON, runs agents, returns raw results
- **The calling agent is the brain** - Designs tests, evaluates results, decides regressions

## Scenario Format

```json
{
  "setup_prompt": "Create a test automation called 'uat_test' with action to turn on light.bed_light.",
  "test_prompt": "Get automation 'automation.uat_test'. Report the result.",
  "teardown_prompt": "Delete automation 'uat_test' if it exists."
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `setup_prompt` | No | Create entities/state needed for the test |
| `test_prompt` | **Yes** | The actual test - exercise tools, report results |
| `teardown_prompt` | No | Cleanup created entities |

Each prompt runs in a separate CLI invocation (fresh context, no PR knowledge).

## CLI Usage

```bash
# Pipe scenario from stdin
echo '{"test_prompt":"Search for light entities. Report how many you found."}' | \
  python tests/uat/run_uat.py --agents gemini

# From file
python tests/uat/run_uat.py --scenario-file /tmp/scenario.json --agents claude,gemini

# Against already-running HA (skip container startup)
python tests/uat/run_uat.py --ha-url http://localhost:8123 --ha-token TOKEN --agents gemini

# Test a specific branch
echo '{"test_prompt":"..."}' | python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini

# Local code (default) vs branch
python tests/uat/run_uat.py                    # uses: uv run --project . ha-mcp
python tests/uat/run_uat.py --branch pr-551    # uses: uvx --from git+...@pr-551 ha-mcp
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--agents` | `claude,gemini` | Comma-separated agent list |
| `--scenario-file` | stdin | Read scenario from file |
| `--ha-url` | (start container) | Use existing HA instance |
| `--ha-token` | test token | HA long-lived access token |
| `--branch` | (local code) | Git branch/tag for ha-mcp |
| `--timeout` | 120 | Timeout per phase in seconds |

## Output Format

JSON to stdout (logs go to stderr):

```json
{
  "scenario": { "setup_prompt": "...", "test_prompt": "...", "teardown_prompt": "..." },
  "ha_url": "http://localhost:54321",
  "mcp_source": "local",
  "branch": null,
  "results": {
    "claude": {
      "available": true,
      "setup": { "completed": true, "output": "...", "duration_ms": 5200, "exit_code": 0 },
      "test": { "completed": true, "output": "...", "duration_ms": 8100, "exit_code": 0 },
      "teardown": { "completed": true, "output": "...", "duration_ms": 2100, "exit_code": 0 }
    },
    "gemini": {
      "available": true,
      "test": { "completed": true, "output": "...", "duration_ms": 6300, "exit_code": 0 }
    }
  }
}
```

### Phase Result Fields

| Field | Description |
|-------|-------------|
| `completed` | Whether the CLI exited with code 0 |
| `output` | Text response from the agent |
| `duration_ms` | Wall clock time |
| `exit_code` | Process exit code |
| `stderr` | Stderr output (MCP debug logs, errors) |
| `num_turns` | Number of agentic turns (if available in JSON output) |
| `tool_stats` | Tool call statistics (if available) |
| `raw_json` | Full raw JSON from the CLI (for deep inspection) |

## Regression Testing

To check if a failure is a regression vs pre-existing:

```bash
# Test the PR branch
echo '{"test_prompt":"..."}' | python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini

# Compare against master
echo '{"test_prompt":"..."}' | python tests/uat/run_uat.py --branch master --agents gemini
```

## Dependencies

Uses existing dev dependencies only:
- `testcontainers` - HA container management
- `requests` - Health check polling
- `tests/initial_test_state/` - Pre-configured HA state
- `tests/test_constants.py` - Test token

## Supported Agents

| Agent | CLI | MCP Config | Notes |
|-------|-----|------------|-------|
| `claude` | `claude` | Temp JSON file via `--mcp-config` | Uses `--permission-mode bypassPermissions` |
| `gemini` | `gemini` | `.gemini/settings.json` in temp cwd | Uses `--approval-mode yolo` |

Unavailable agents are skipped with a warning (no error).
