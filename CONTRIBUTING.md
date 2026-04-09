# Contributing to Home Assistant MCP Server

Thank you for your interest in contributing!

## 🚀 Quick Start

1. **Fork and clone** the repository
2. **Install**: `uv sync --group dev`
3. **Install hooks**: `uv run lefthook install`
4. **Test**: `uv run pytest tests/src/e2e/ -n2 --dist loadscope -v` (requires Docker)
5. **Make changes** and commit
6. **Open Pull Request**

## 🧪 Testing

See **[tests/README.md](tests/README.md)**.

## 🛠️ Development

**Setup:**
```bash
cp .env.example .env    # Edit with your HA details
uv sync --group dev
uv run lefthook install    # Install git hooks
```

**Code quality:**
```bash
uv run ruff format src/ tests/     # Format
uv run ruff check --fix src/ tests/ # Lint
uv run mypy src/                   # Type check
uv run ast-grep scan               # AST lint (error handling patterns)
```

On every commit, hooks run `ruff check --fix` (lint), `ast-grep scan` (AST lint), `mypy` (type check), and unit tests in parallel via [lefthook](https://github.com/evilmartians/lefthook). The **Ruff Lint** and **AST Lint** CI jobs also enforce this on pull requests.

## 🔄 Migrating from pre-commit to lefthook

If you had pre-commit installed from a previous checkout:

```bash
uv run pre-commit uninstall
uv sync --group dev
uv run lefthook install --reset-hooks-path
```

## 📋 Guidelines

- **Code**: Follow existing patterns, add type hints, test new features
- **Docs**: Update README.md for user-facing changes
- **PRs**: Use the template, ensure tests pass

## 🏗️ Stuck?

- Open an [Issue](../../issues).
- See **[AGENTS.md](AGENTS.md)** for additional tips.

Thank you for contributing! 🎉