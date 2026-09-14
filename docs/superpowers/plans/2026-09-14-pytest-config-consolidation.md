# Pytest Configuration Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the repository's two divergent pytest configurations with one complete configuration in `pyproject.toml`.

**Architecture:** Treat the configuration currently loaded by CI and lefthook as the behavioral source of truth, translate it to TOML, and keep root-relative test discovery. Remove the nested INI file so every invocation walks to the same root configuration, then update the two current documents that name the old owner.

**Tech Stack:** Python 3.13, pytest 8, pytest-asyncio, pytest-timeout, TOML, uv

## Global Constraints

- Work only on branch `fix/pytest-config-consolidation` in `worktree/fix-pytest-config`.
- Do not modify test logic, dependencies, CI workflows, lefthook commands, or source code.
- Preserve the effective behavior currently defined by `tests/pytest.ini`.
- Keep root-relative discovery as `testpaths = ["tests"]`.
- Use the current failing root collection command as the RED signal; this configuration-only change does not add a test module.
- Do not push or open a pull request without separate user approval.

---

### Task 1: Consolidate pytest configuration and documentation

**Files:**

- Modify: `pyproject.toml:258`
- Delete: `tests/pytest.ini`
- Modify: `tests/README.md:68`
- Modify: `docs/agents/development.md:64`

**Interfaces:**

- Consumes: pytest configuration discovery from the repository root and `tests/` directory.
- Produces: one `[tool.pytest.ini_options]` table selected by every supported invocation.

- [ ] **Step 1: Install the locked development environment**

Run from the worktree root:

```bash
uv sync --group dev
```

Expected: exit 0 with the repository environment installed from `uv.lock`.

- [ ] **Step 2: Verify the current root invocation is RED**

Run:

```bash
uv run pytest --collect-only -q
```

Expected: non-zero exit during collection, including `'haos_only' not found in markers configuration option`. Record the exact collection and error counts; do not treat unrelated import failures as proof of this issue.

- [ ] **Step 3: Replace the partial TOML pytest table with the complete effective configuration**

In `pyproject.toml`, keep `testpaths = ["tests"]` and replace the remaining `[tool.pytest.ini_options]` values with this TOML shape:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
empty_parameter_set_mark = "fail_at_collect"
addopts = [
    "--strict-markers",
    "--strict-config",
    "--verbose",
    "--tb=short",
    "--maxfail=3",
    "-ra",
    "--durations=10",
]
markers = [
    "slow: marks tests as slow (use -m 'not slow' to skip)",
    "addon_disruptive: restarts the server-under-test's own process (addon self-restart); must never run concurrently with other tests on lanes where the MCP server is out-of-process - the lane workflow runs these in a single-worker phase AFTER the parallel run",
    "unit: unit tests (isolated, fast)",
    "integration: integration tests requiring external services",
    "e2e: end-to-end tests with full system",
    "performance: performance and load tests",
    "automation: automation lifecycle tests",
    "blueprint: blueprint management tests",
    "calendar: calendar event management tests",
    "device: device control tests",
    "script: script orchestration tests",
    "helper: helper integration tests",
    "integrations: integration management tests (config entries)",
    "labels: label management tests",
    "registry: entity and device registry tests",
    "services: service discovery tests",
    "todo: todo/shopping list tests",
    "convenience: convenience tools tests (scene, weather, energy, docs)",
    "updates: update management tools tests",
    "system: system management tests (restart, reload, health)",
    "themes: frontend theme management tests",
    "error_handling: error handling and edge case tests",
    "cleanup: tests that create entities needing cleanup",
    "area: area management tests",
    "floor: floor management tests",
    "zone: zone management tests",
    "group: entity group management tests",
    "hacs: HACS (Home Assistant Community Store) tests",
    "update_path: component (stable tag + working tree) x new-server in-process update-path e2e (dedicated lane; runs only with E2E_UPDATE_PATH=1; #1783/#1785)",
    "core: core tool tests (state, service, bulk control, history, templates)",
    "config: configuration management tests (helpers, labels)",
    "filesystem: filesystem access tools tests",
    "haos_only: skip unless HAOS_TEST_IMAGE_PATH is set (HAOS-QEMU backend required; #1281)",
    "beta_haos_only: beta HAOS image lanes with Supervisor/Core version expectations configured",
    "container_only: skip when HAOS backend is active (testcontainer-specific behavior; #1281)",
    "embedded_only: only the embedded testcontainer backend (E2E_BACKEND=embedded), the one lane that installs ha-mcp into the HA image (#2245)",
    "external_only: in-process-server tiers only - skip for inaddon, stdio, container-embedded, and HAOS-embedded servers; #1349, #1527, #2233",
    "inaddon_only: HAOS inaddon mode only - exercises is_running_in_addon()=True paths; #1349",
    "haos_stdio_only: HAOS stdio mode only - exercises the installed ha-mcp command over a real stdio subprocess transport; #2233",
    "haos_tls: final HAOS embedded scenario that restarts Core with HTTPS in the existing worker VM, then restores HTTP; #2241",
    "haos_embedded_only: HAOS embedded mode only (HAOS_TEST_MODE=embedded) - the server shares Home Assistant's process and event loop; #2357",
    "not_on_embedded: skip on the embedded backend (E2E_BACKEND=embedded) - provably redundant with the lane's own in-process ha_mcp_server session backend; #1527",
    "not_on_haos_embedded: skip on the haos_embedded backend (HAOS_TEST_MODE=embedded) - provably redundant with the lane's own session backend, which enables the entry once and drives the in-process server for the whole suite; #1527",
    "requires_tools_entry: needs the File & YAML Tools config entry - skipped on E2E_NO_TOOLS_ENTRY=1 lanes (#2292)",
    "no_tools_only: runs only on E2E_NO_TOOLS_ENTRY=1 lanes (#2292)",
]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
timeout = 300
timeout_method = "signal"
timeout_func_only = true
log_cli = true
log_cli_level = "INFO"
log_cli_format = "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
log_cli_date_format = "%Y-%m-%d %H:%M:%S"
filterwarnings = [
    "error",
    "ignore::DeprecationWarning",
    "ignore::PendingDeprecationWarning",
    "ignore::ResourceWarning",
    "ignore::DeprecationWarning:testcontainers",
]
```

Preserve the existing explanatory comments by moving them beside the equivalent TOML keys. Use plain ASCII hyphens inside TOML strings if copying an em dash would make formatting inconsistent; marker names and behavior must remain unchanged.

- [ ] **Step 4: Remove the nested configuration owner**

Delete `tests/pytest.ini`. Do not replace it with a forwarding file or symlink.

- [ ] **Step 5: Update the two current documentation references**

In `tests/README.md`, remove `pytest.ini` from the `tests/` tree and add this sentence immediately after the tree:

```markdown
Pytest configuration lives in the repository root `pyproject.toml` so every invocation uses the same markers and runtime settings.
```

In `docs/agents/development.md`, replace:

```markdown
`tests/pytest.ini` sets `--maxfail=3`, so
```

with:

```markdown
The root `pyproject.toml` sets `--maxfail=3`, so
```

- [ ] **Step 6: Verify TOML syntax and stale references**

Run:

```bash
uv run python -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb'))"
git grep -n "tests/pytest.ini" -- pyproject.toml tests/README.md docs/agents/development.md
```

Expected: TOML command exits 0. Grep exits 1 with no output because the three owner documents contain no stale reference.

- [ ] **Step 7: Verify both pytest discovery paths are GREEN**

Run from the worktree root:

```bash
uv run pytest --collect-only -q
uv run pytest tests/src/unit/test_config.py --collect-only -q
```

Then run from `tests/`:

```bash
uv run pytest --collect-only -q src/unit
```

Expected: all three commands exit 0, use the root `pyproject.toml`, and report no unknown-marker or strict-configuration errors. Record collection counts separately; do not claim runtime tests passed from collection-only evidence.

- [ ] **Step 8: Review the exact diff**

Run:

```bash
git diff --check
git status --short
git diff -- pyproject.toml tests/pytest.ini tests/README.md docs/agents/development.md
```

Expected: no whitespace errors and only the four scoped files changed, in addition to the already committed design and plan documents.

- [ ] **Step 9: Commit the implementation**

Run:

```bash
git add pyproject.toml tests/pytest.ini tests/README.md docs/agents/development.md
git commit -m "test: consolidate pytest configuration"
```

Expected: one focused implementation commit after the design and plan documentation commits.
