# BAT Framework - Bot Acceptance Testing

Executes MCP test scenarios on real AI agent CLIs (Claude, Gemini, OpenAI-compatible) against a Home Assistant test instance. Designed to be driven by a calling agent that generates scenarios dynamically, runs them, and evaluates results.

## Quick Start

Two runners, two use cases:

```bash
# Run the pre-built story catalog (most common)
uv run python tests/uat/stories/run_story.py --all --agents gemini

# Run one ad-hoc scenario (must pipe JSON via stdin or use --scenario-file)
echo '{"test_prompt":"Search for light entities. Report how many you found."}' \
  | uv run python tests/uat/run_uat.py --agents gemini
```

Commands must be prefixed with `uv run python` — the repo targets Python 3.13 via uv.

For OpenAI-compatible endpoints (LM Studio, Ollama, vLLM), the `--base-url` must include the `/v1` suffix, e.g. `http://172.19.0.1:1234/v1`. LM Studio requires the model to be loaded in its UI first; Ollama auto-loads on demand.

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
  |<-- returns summary JSON -----------|                        |
  |    (full results in temp file)     |                        |
  |                                                             |
  |-- evaluates pass/fail                                       |
  |-- reads full results only if needed                         |
```

- **No pre-built scenarios** - The calling agent generates them based on what it's testing
- **The runner is a dumb executor** - Takes JSON, runs agents, returns raw results
- **The calling agent is the brain** - Designs tests, evaluates results, decides regressions
- **Progressive disclosure** - Summary on stdout, full results in a temp file

## Scenario Format

```json
{
  "setup_prompt": "Create a test automation called 'bat_test' with action to turn on light.bed_light.",
  "test_prompt": "Get automation 'automation.bat_test'. Report the result.",
  "teardown_prompt": "Delete automation 'bat_test' if it exists."
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
  uv run python tests/uat/run_uat.py --agents gemini

# From file
uv run python tests/uat/run_uat.py --scenario-file /tmp/scenario.json --agents claude,gemini

# Against already-running HA (skip container startup)
uv run python tests/uat/run_uat.py --ha-url http://localhost:8123 --ha-token TOKEN --agents gemini

# Test a specific branch
echo '{"test_prompt":"..."}' | uv run python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini

# Local code (default) vs branch
uv run python tests/uat/run_uat.py                    # uses: uv run --project . ha-mcp
uv run python tests/uat/run_uat.py --branch pr-551    # uses: uvx --from git+...@pr-551 ha-mcp

# OpenAI-compatible local LLM (LM Studio, Ollama, vLLM, etc.)
echo '{"test_prompt":"..."}' | uv run python tests/uat/run_uat.py --agents openai --base-url http://localhost:1234/v1

# With a specific model and API key
echo '{"test_prompt":"..."}' | uv run python tests/uat/run_uat.py --agents openai \
  --base-url http://localhost:1234/v1 --model my-model --api-key sk-xxx
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--agents` | `claude,gemini` | Comma-separated agent list |
| `--scenario-file` | stdin | Read scenario from file |
| `--ha-url` | (start container) | Use existing HA instance |
| `--ha-token` | test token | HA long-lived access token |
| `--branch` | (local code) | Git branch/tag for ha-mcp |
| `--timeout` | 300 | Timeout per phase in seconds |
| `--base-url` | — | OpenAI-compatible API base URL (required for `openai` agent) |
| `--api-key` | `no-key` | API key for OpenAI-compatible endpoint |
| `--model` | (auto-detect) | Model name (auto-detected from `/v1/models` for openai) |

## Output Format

### Stdout: Concise Summary

The calling agent receives a compact summary. It leaves out the raw JSON to save context:

```json
{
  "mcp_source": "local",
  "branch": null,
  "results_file": "/tmp/bat_results_abc123.json",
  "agents": {
    "gemini": {
      "available": true,
      "all_passed": true,
      "setup": {
        "completed": true,
        "duration_ms": 5200,
        "exit_code": 0,
        "output": "...",
        "num_turns": 3,
        "tool_stats": { "totalCalls": 2, "totalSuccess": 2, "totalFail": 0 }
      },
      "test": {
        "completed": true,
        "duration_ms": 8100,
        "exit_code": 0,
        "output": "...",
        "num_turns": 5,
        "tool_stats": { "totalCalls": 4, "totalSuccess": 4, "totalFail": 0 }
      },
      "teardown": {
        "completed": true,
        "duration_ms": 2100,
        "exit_code": 0,
        "output": "...",
        "num_turns": 2,
        "tool_stats": { "totalCalls": 1, "totalSuccess": 1, "totalFail": 0 }
      },
      "aggregate": {
        "total_duration_ms": 15400,
        "total_turns": 10,
        "total_tool_calls": 7,
        "total_tool_success": 7,
        "total_tool_fail": 0
      }
    }
  }
}
```

**Aggregate stats** provide overall efficiency metrics for comparing branches:
- `total_duration_ms` - Total wall clock time across all phases
- `total_turns` - Total agentic turns (available for Claude, Gemini, OpenAI)
- `total_tool_calls` - Total MCP tool invocations
- `total_tool_success` / `total_tool_fail` - Success/failure counts

Every phase summary includes `output`, plus `tool_trace` when tool calls were logged. A failed phase also includes `stderr` when it is not empty:

```json
{
  "test": {
    "completed": false,
    "duration_ms": 120000,
    "exit_code": -1,
    "output": "",
    "stderr": "Timed out after 120s"
  }
}
```

### Full Results File

The `results_file` path points to a temp file with everything: full agent output, raw JSON, stderr, scenario. The calling agent reads this only when it needs to dig deeper (e.g. inspecting exact tool responses on failure).

### Phase Result Fields (full results)

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
echo '{"test_prompt":"..."}' | uv run python tests/uat/run_uat.py --branch feat/tool-errors --agents gemini

# Compare against master
echo '{"test_prompt":"..."}' | uv run python tests/uat/run_uat.py --branch master --agents gemini
```

## Dependencies

Uses dev dependencies:
- `testcontainers` - HA container management
- `requests` - Health check polling
- `openai` - OpenAI-compatible API client (for openai agent)
- `tests/initial_test_state/` - Pre-configured HA state
- `tests/test_constants.py` - Test token

## Supported Agents

| Agent | CLI | MCP Config | Notes |
|-------|-----|------------|-------|
| `claude` | `claude` | Temp JSON file via `--mcp-config` | Uses `--permission-mode bypassPermissions` |
| `gemini` | `gemini` | `.gemini/settings.json` in temp cwd | Uses `--approval-mode yolo` |
| `openai` | `openai_agent.py` | Temp JSON file via `--mcp-config` | Any OpenAI-compatible API (LM Studio, Ollama, vLLM, etc.). Requires `--base-url`. |
| `codex` | `codex exec` | Private, temporary Codex profile | Requires `--model`, Codex CLI and a dedicated `CODEX_HOME/auth.json`. Shell and hosted apps are disabled. |

## Codex on the Actions bench

The manual `BAT Codex story` workflow in `ha-mcp-workflows-dev` checks out a
maintainer-supplied full `ha-mcp` commit SHA. It selects `gpt-6-astra`,
`gpt-6-sol`, or `gpt-5.6-terra` and runs one existing story against a disposable
Home Assistant container. The bench supplies its own OAuth credential; no
product issue, PR or branch is modified. The story runner performs deterministic
setup and `ha_checks`, and records the model, source SHA, response, observed MCP
tool calls, turns and token usage in its normal JSONL result. The workflow
publishes only a compact result row to the Actions summary. Run each model
separately to compare results on the same SHA and story.

For a local run with a test credential:

```bash
uv run python tests/uat/stories/run_story.py \
  tests/uat/stories/catalog/s01_automation_sunset_lights.yaml \
  --agents codex --model gpt-6-sol
```

The BAT adapter copies `CODEX_HOME/auth.json` into a private directory per
scenario, creates a strict workspace profile containing only the BAT's stdio MCP
server, and starts a fresh ephemeral Codex process for every phase. The writable
workspace is an empty temporary directory, never the repository. The OAuth
directory is explicitly denied and probed with `codex sandbox` before the
model runs. Shell, command network and hosted apps are disabled; the MCP server
receives only its explicit HA test connection variables. A rotated auth file is copied back to the caller's
dedicated `CODEX_HOME` for the bench's separate persistence step. Do not point
`CODEX_HOME` at a personal or product OAuth profile when using the bench.

This is agent behavior testing of `ha-mcp` tools. A single story is evidence for
that story and model only. It does not test the slash agent's GitHub admission,
coding, publication, review continuation or readiness logic. The benchmark
does not run automatically, and model nondeterminism means one pass cannot
establish regression freedom. For comparisons, hold the story, model, CLI
version and HA image fixed, run baseline and candidate SHAs separately, then
inspect the recorded tool sequence and HA checks. The Codex adapter uses the
CLI's JSON events; unsupported or missing event fields remain absent instead
of being estimated.

The alternatives considered were:

| Connection | Value | Tradeoff and decision |
|---|---|---|
| Attach disposable HA to `/astra`, `/sol`, `/terra` coding turns | Focused developer smoke testing | Mixes BAT fixture state and task context, lacks a fresh blind agent run and independent scoring; omitted. |
| Let the slash coding worker design and judge BAT scenarios | Adaptive coverage | Shares the coding worker's context and potential bias, consumes more model calls, and needs a separate trusted evaluator; deferred. |
| Give the agent a general runner to orchestrate multi-phase BAT work | Flexible experiments | Expands runtime and credential surface and makes runs harder to reproduce; deferred. |
| Run Codex as a BAT agent on the manual bench | Exercises the actual MCP client behavior with existing setup, checks and metrics | One explicit story per dispatch limits cost and keeps the slash controller unchanged; implemented. |

The implemented path spends one model invocation for each populated BAT phase
and one HA container per agent, plus container startup and Codex CLI setup on
the runner. Astra, Sol and Terra have separate inference cost and may produce
different tool paths. The other options would add model calls inside ordinary
slash coding turns or require a second model to judge generated scenarios.
The chosen path keeps the slash App token and its publication runner out of
scope; only the bench's dedicated OAuth session and disposable HA token are
present. The exact product SHA, story, HA image and CLI version are recorded or
pinned, while model responses remain nondeterministic.

Unavailable agents are skipped with a warning (no error). The `openai` agent is always available since it's a local script.
