# UAT Testing

UAT stories always use testcontainers — a fresh HA Docker container is spun up per agent run. Never use `--ha-url` pointing at a production HA instance.

When testcontainers fails with an image error, diagnose Docker first (`docker images`, `docker info`) before suggesting alternatives. The HA test image is cached locally; a "No such image" error in WSL2 is usually a Docker context mismatch, not a missing image.

## Running stories

```bash
# All stories, local model (LM Studio)
UV_CACHE_DIR=/tmp/claude-1000/uv-cache TMPDIR=/tmp/claude-1000 \
  uv run python tests/uat/stories/run_story.py --all --agents openai \
  --base-url http://172.19.0.1:1234/v1 --model <model-id> --no-think

# With a feature flag
... --mcp-env ENABLE_LITE_DOCSTRINGS=true
```

Key flags:
- `--mcp-env KEY=VALUE` — pass env vars to the MCP server (repeatable)
- `--no-think` — disable reasoning mode for qwen3 and compatible models
- `--results-file` — JSONL file to append results to (default: `local/uat-results.jsonl`)
