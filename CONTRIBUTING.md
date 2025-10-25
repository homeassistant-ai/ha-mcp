# Contributing to Home Assistant MCP Server

Thank you for your interest in contributing!

## 🚀 Quick Start

1. **Fork and clone** the repository
2. **Install**: `uv sync --group dev`
3. **Test**: `uv run pytest tests/src/e2e/ -v` (requires Docker)
4. **Make changes** and commit
5. **Open Pull Request**

## 🧪 Testing

See **[tests/README.md](tests/README.md)**.

## 🛠️ Development

**Setup:**
```bash
cp .env.example .env    # Edit with your HA details
uv sync --group dev
```

**Code quality:**
```bash
uv run ruff format src/ tests/     # Format
uv run ruff check --fix src/ tests/ # Lint
uv run mypy src/                   # Type check
```

## 📊 Tool Usage Logging

Set `HOMEASSISTANT_TOOL_LOG_DIR=artifacts` to capture every tool request and response in
the server logs. This is useful when diagnosing overly verbose tools or
preparing optimization work. Detailed usage instructions, including the
analysis helper script, live in
[`docs/tool-usage-logging.md`](docs/tool-usage-logging.md). The `E2E Tests`
workflow (pushes) and the `PR Validation Pipeline` workflow (pull requests)
enforce this by writing the combined pytest telemetry to
`artifacts/tool_calls.ndjson.zst` and running `tests/src/tool_logging/` to ensure
the analysis helpers keep working. The PR pipeline calls the same reusable `E2E Tests`
workflow so the log instrumentation never drifts between push and PR runs.

## 📋 Guidelines

- **Code**: Follow existing patterns, add type hints, test new features
- **Docs**: Update README.md for user-facing changes
- **PRs**: Use the template, ensure tests pass

## 🏗️ Stuck?

- Open an [Issue](../../issues).
- See **[AGENTS.md](AGENTS.md)** for additional tips.

Thank you for contributing! 🎉