# Pytest Configuration Consolidation Design

Issue: [#2432](https://github.com/homeassistant-ai/ha-mcp/issues/2432)

## Problem

The repository has two independent pytest configurations. Root-level pytest invocations load `pyproject.toml`, while invocations rooted under `tests/` load `tests/pytest.ini`. The files do not merge and do not define the same markers, warning policy, asyncio loop scopes, logging, or default options. As a result, the documented root command can fail during collection even though CI and lefthook use the other configuration successfully.

## Goal

Make every supported pytest invocation discover one authoritative configuration with the behavior currently used by CI and lefthook.

## Assumptions

- `tests/pytest.ini` is the behavioral source of truth because automated test lanes currently load it.
- Root-relative test discovery remains `testpaths = ["tests"]`.
- Node IDs may gain the `tests/` prefix after pytest selects the repository root. No repository code consumes node IDs as a stable interface.
- This change does not alter test logic, dependencies, or CI commands.

## Design

Expand `[tool.pytest.ini_options]` in `pyproject.toml` so it contains the complete effective configuration from `tests/pytest.ini`. Convert multiline INI values into TOML arrays or strings without changing their order or meaning.

Keep the existing root-relative discovery settings in `pyproject.toml`. Preserve all markers and runtime settings from `tests/pytest.ini`, including strict collection, `--maxfail=3`, summary and duration output, session-scoped asyncio loops, per-test timeout behavior, live logging, and warning filters.

Delete `tests/pytest.ini`. Pytest will then walk upward from both repository-root and `tests/` invocations and select `pyproject.toml`.

Update the two direct documentation references that describe `tests/pytest.ini` as the configuration owner:

- `tests/README.md` should point to root `pyproject.toml`.
- `docs/agents/development.md` should attribute `--maxfail=3` to `pyproject.toml`.

## Alternatives Rejected

Keeping `tests/pytest.ini` and adding `-c tests/pytest.ini` to every command leaves command correctness dependent on callers and does not fix ad hoc contributor invocations.

Keeping two synchronized copies fixes the immediate mismatch but preserves the drift mechanism that caused the issue.

Removing the pytest section from `pyproject.toml` and retaining only `tests/pytest.ini` is less reliable for root discovery because `pyproject.toml` remains at the repository root and modern pytest may select it as an empty fallback configuration.

## Verification

Use configuration behavior itself as the regression signal; no new test module is needed for a configuration-only change.

Before the change:

```bash
uv run pytest --collect-only -q
```

The root invocation must reproduce the strict-marker failure described in the issue.

After the change, verify both entry paths:

```bash
uv run pytest --collect-only -q
cd tests && uv run pytest --collect-only -q src/unit
```

Both commands must select the root `pyproject.toml`, recognize the complete marker set, and finish collection without configuration errors.

Then run focused structural and quality checks:

```bash
uv run pytest tests/src/unit/test_config.py --collect-only -q
uv run python -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb'))"
git grep -n "tests/pytest.ini" -- pyproject.toml tests/README.md docs/agents/development.md
```

The TOML parse must succeed. The grep must return no stale references in the three current owner documents.

## Scope

Expected pull request files:

- `pyproject.toml`
- deleted `tests/pytest.ini`
- `tests/README.md`
- `docs/agents/development.md`

No source code, test cases, dependencies, workflows, or unrelated documentation are in scope.
