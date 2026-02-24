# Contributing to Home Assistant MCP Server

Thank you for your interest in contributing!

## 🚀 Quick Start

1. **Fork and clone** the repository
2. **Install**: `uv sync --group dev`
3. **Install hooks**: `uv run pre-commit install`
4. **Test**: `uv run pytest tests/src/e2e/ -v` (requires Docker)
5. **Make changes** and commit
6. **Open Pull Request**

## 🧪 Testing

See **[tests/README.md](tests/README.md)**.

## 🛠️ Development

**Setup:**
```bash
cp .env.example .env    # Edit with your HA details
uv sync --group dev
uv run pre-commit install  # Install pre-commit hooks
```

**Code quality:**
```bash
uv run ruff format src/ tests/     # Format
uv run ruff check --fix src/ tests/ # Lint
uv run mypy src/                   # Type check
```

On every commit, a `pre-commit` hook runs `ruff check --fix` to auto-fix and catch lint violations. The **Ruff Lint** CI job also enforces this on pull requests.

## 📋 Guidelines

- **Code**: Follow existing patterns, add type hints, test new features
- **Docs**: Update README.md for user-facing changes
- **PRs**: Use the template, ensure tests pass

## 🏗️ Stuck?

- Open an [Issue](../../issues).
- See **[AGENTS.md](AGENTS.md)** for additional tips.

Thank you for contributing! 🎉