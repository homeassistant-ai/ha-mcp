# Tool Usage Logging and Analysis

The Home Assistant MCP server can emit detailed tool request and response logs to
help identify commands that generate excessive traffic. These logs are
particularly useful when optimizing token usage.

## Enabling Verbose Tool Logging

Set the `HOMEASSISTANT_LOG_ALL` environment variable to `true` before starting
the MCP server. When the setting is active, every tool invocation will emit a
single structured JSON entry that contains:

- The tool name and execution status
- Serialized request arguments and keyword arguments
- Serialized tool response (when available)
- Character counts for both the request and response payloads

When `HOMEASSISTANT_LOG_ALL` is active the server writes the entries to
`artifacts/tool_calls.ndjson.zst` (configurable via `TOOL_LOG_PATH`) using an
async `QueueHandler`/`QueueListener` pipeline to avoid blocking the request
thread. Each line in the file is a standalone JSON object compressed with
Zstandard:

```json
{"duration_ms": 12.427, "event": "tool_call", "request": {"args": [], "kwargs": {"domain": "light", "service": "turn_on"}}, "request_characters": 97, "response": {"content": [{"type": "text", "text": "done"}]}, "response_characters": 63, "status": "success", "tool": "ha_call_service"}
```

## Analyzing Tool Logs

The repository ships with `scripts/tool_log_stats.py` to help crunch the log
output. The script works directly on the generated `.ndjson.zst` files and
streams entries without loading the entire artifact into memory.

Key commands:

```bash
# Summarize average request/response sizes (character counts)
python scripts/tool_log_stats.py summary /path/to/tool_calls.ndjson.zst

# Largest response across the entire log
python scripts/tool_log_stats.py largest /path/to/tool_calls.ndjson.zst

# Restrict to a single tool name
python scripts/tool_log_stats.py largest /path/to/tool_calls.ndjson.zst --tool ha_call_service

# Switch to token-based metrics (requires the optional tiktoken package)
python scripts/tool_log_stats.py summary /path/to/tool_calls.ndjson.zst --tokens --encoding cl100k_base
```

The `summary` command prints per-tool averages plus the largest recorded
response. The `largest` command prints the full request and response payloads
for the single most verbose entry, with an optional `--tool` filter.

Token counting is optional; if the `tiktoken` package is not installed the
script falls back to character counts.

## Continuous Integration Validation

The `E2E Tests` GitHub workflow (pushes) and the `PR Validation Pipeline`
workflow (pull requests) automatically enable `HOMEASSISTANT_LOG_ALL=true` and
store the combined pytest telemetry in `artifacts/tool_calls.ndjson.zst`. Each
run invokes the follow-up `tool_logging` test suite, which consumes the
artifact and runs the helpers above to ensure at least one successful tool call
was captured.
Pull requests reuse the same `E2E Tests` workflow via `workflow_call`, keeping
log instrumentation in sync without maintaining duplicate job definitions.
Workflow failures flag regressions in verbose logging or the analysis pipeline
long before optimization work begins.

## Storage and Retention Tips

- Tool logs are emitted through an asynchronous logging queue and compressed
  with Zstandard. Set `TOOL_LOG_PATH` if you need to capture them in a different
  location or wish to segment runs.
- Because each tool call is serialized to JSON (one line per call), the logs can
  be ingested by log management platforms for long-term retention.
- The `.zst` compression keeps artifacts compact, but consider pruning the
  directory when verbose logging is enabled for extended periods.
