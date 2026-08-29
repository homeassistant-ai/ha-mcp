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

On every commit, hooks run `ruff check --fix` (lint), `ast-grep scan` (AST lint), `mypy` (type check), and unit tests in parallel via [lefthook](https://github.com/evilmartians/lefthook). On pull requests the **Fast Checks** CI job enforces the lint, AST lint, and type checks (unit tests run in the separate **Unit Tests** job), alongside the lockfile, docs-size, HACS and Hassfest checks, and additionally runs `ruff format --check` on changed Python files.

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
- **Bot reviews**: Address every CodeRabbit finding, inline and top-level.
  CodeRabbit nests some findings (Outside diff range, Nitpick) inside
  collapsed sections of its review bodies rather than inline threads — read
  the full bodies. See AGENTS.md § PR Review Comments for the sweep
- **Translations**: A language ships on all four translated surfaces or not at
  all. Read `src/ha_mcp/settings_ui/locales/README.md` first. Changing an
  English string owes no translations — the post-merge locale-sync workflow
  machine-fills them daily, and its verification rejects a catalog left
  byte-identical to English

## 💤 Abandoned PRs

If a maintainer requests changes or an update on a PR and there is no response or
activity from the author for **7 days**, a maintainer may, at their discretion,
take over the PR (push to it or supersede it) or close it.

- An automated reminder is posted on the PR after 4 and 6 days without a response.
- Any activity from the author (a comment is enough) resets the clock.
- This is a guideline, not a hard rule — maintainers may leave a PR open longer
  when the situation warrants it.

## 🏗️ Stuck?

- Open an [Issue](../../issues).
- See **[AGENTS.md](AGENTS.md)** for additional tips.

Thank you for contributing! 🎉