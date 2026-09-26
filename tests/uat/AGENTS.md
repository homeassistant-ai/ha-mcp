# UAT Testing

UAT stories always use testcontainers — a fresh HA Docker container is spun up per agent run. Never use `--ha-url` pointing at a production HA instance.

When testcontainers fails with an image error, diagnose Docker first (`docker images`, `docker info`) before suggesting alternatives. The HA test image is cached locally; a "No such image" error in WSL2 is usually a Docker context mismatch, not a missing image.

## Running stories

Stories live in `tests/uat/stories/catalog/`. `--all` runs only the `s*.yaml` stories; run the `c*` and `t*` stories by path. Run from the repo root.

```bash
# Single story (path must be relative to repo root)
uv run python tests/uat/stories/run_story.py \
  tests/uat/stories/catalog/s01_automation_sunset_lights.yaml \
  --agents openai --base-url "<openai-compatible-base-url>" --model "<model-id>"

# All stories, local OpenAI-compatible model
uv run python tests/uat/stories/run_story.py --all --agents openai \
  --base-url "<openai-compatible-base-url>" --model "<model-id>" --no-think

# With a feature flag
... --mcp-env ENABLE_LITE_DOCSTRINGS=true
```

Key flags:
- `--mcp-env KEY=VALUE` — pass env vars to the MCP server (repeatable). The server gets a fresh, empty `HA_MCP_CONFIG_DIR`, so tool pins and settings in your `~/.ha-mcp` do not apply; pass `--mcp-env HA_MCP_CONFIG_DIR=<dir>` to run with a specific config.
- `--no-think` disables reasoning: prepends /no_think (original Qwen3) and sends the enable_thinking=false chat-template kwarg (Qwen3.5/3.6)
- `--results-file` — JSONL file to append results to (default: `local/uat-results.jsonl`)
