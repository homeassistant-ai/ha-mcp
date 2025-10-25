# Tool Usage Logging and Analysis

The Home Assistant MCP server can emit detailed tool request and response logs to
help identify commands that generate excessive traffic. These logs are
particularly useful when optimizing token usage.

## Enabling Verbose Tool Logging

Set the `HOMEASSISTANT_LOG_ALL` environment variable to `true` before starting
the MCP server. When the setting is active, every tool invocation will emit a
single log line that contains:

- The tool name and execution status
- Serialized request arguments and keyword arguments
- Serialized tool response (when available)
- Character counts for both the request and response payloads

Example log entry:

```text
2024-05-15 10:32:11,554 [INFO] ha_mcp.server [TOOL_CALL] {"event": "tool_call", "tool": "ha_call_service", "status": "success", "request": {"args": [], "kwargs": {"domain": "light", "service": "turn_on"}}, "request_characters": 95, "response": {"success": true}, "response_characters": 28}
```

## Analyzing Tool Logs

The repository ships with `scripts/tool_log_stats.py` to help crunch the log
output. The script works on any log file that contains the `[TOOL_CALL]` marker
generated when verbose logging is enabled.

Key commands:

```bash
# Summarize average request/response sizes (character counts)
python scripts/tool_log_stats.py summary /path/to/server.log

# Largest response across the entire log
python scripts/tool_log_stats.py largest /path/to/server.log

# Restrict to a single tool name
python scripts/tool_log_stats.py largest /path/to/server.log --tool ha_call_service

# Switch to token-based metrics (requires the optional tiktoken package)
python scripts/tool_log_stats.py summary /path/to/server.log --tokens --encoding cl100k_base
```

The `summary` command prints per-tool averages plus the largest recorded
response. The `largest` command prints the full request and response payloads
for the single most verbose entry, with an optional `--tool` filter.

Token counting is optional; if the `tiktoken` package is not installed the
script falls back to character counts.

## Continuous Integration Validation

The `E2E Tests` GitHub workflow (pushes) and the `PR Validation Pipeline`
workflow (pull requests) automatically enable `HOMEASSISTANT_LOG_ALL=true` and
store the combined pytest output in `artifacts/tool_calls.log`. Each run invokes
the follow-up `tool_logging` test suite, which consumes the artifact and runs
the helpers above to ensure at least one successful tool call was captured.
Pull requests reuse the same `E2E Tests` workflow via `workflow_call`, keeping
log instrumentation in sync without maintaining duplicate job definitions.
Workflow failures flag regressions in verbose logging or the analysis pipeline
long before optimization work begins.

## Storage and Retention Tips

- Tool logs inherit the standard Python logging configuration. Redirect output
  to a dedicated file (for example via `python -m ha_mcp >> tool.log`) to keep
  data organized.
- Because each tool call is serialized to JSON, the logs can also be ingested
  by log management platforms for long-term retention.
- Consider rotating logs regularly to keep file sizes manageable when verbose
  logging is enabled for extended periods.
