# Development reference

Read this document when setting up the repository, running the server, choosing
verification commands, researching Home Assistant APIs, or orienting yourself
in the architecture. Behavioral rules about when testing is required remain in
[`AGENTS.md`](../../AGENTS.md); test implementation details live in
[`tests/AGENTS.md`](../../tests/AGENTS.md).

## External documentation

| Resource | Use |
|---|---|
| [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest) | Entity states, services, and configuration. |
| [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket) | Events, subscriptions, and in-process commands. |
| [Home Assistant Core](https://github.com/home-assistant/core) | Undocumented or version-specific behavior. |
| [Home Assistant app development](https://developers.home-assistant.io/docs/apps) | App packaging and configuration. |
| [FastMCP](https://gofastmcp.com/getting-started/welcome) | MCP server framework. |
| [Model Context Protocol](https://modelcontextprotocol.io/docs) | Protocol semantics. |

Prefer current primary sources. When local behavior depends on an undocumented
Home Assistant detail, verify it against the exact upstream source or a focused
runtime test rather than relying on memory.

## Setup and server commands

```bash
uv sync --group dev
cp .env.example .env
uv run ha-mcp
uv run ha-mcp-web
```

`ha-mcp` uses stdio and needs interactive stdin. `ha-mcp-web` runs HTTP mode
and serves the settings UI at `http://localhost:8086/mcp/settings`. Settings UI
details are in
[`src/ha_mcp/settings_ui/AGENTS.md`](../../src/ha_mcp/settings_ui/AGENTS.md).

## Test commands

E2E tests live in `tests/src/e2e/` (not `tests/e2e/`), use testcontainers,
and require a running Docker daemon. Launch them from `tests/` so pytest
loads the correct `conftest.py`. Unit tests in `tests/src/unit/` do not use
Docker.

```bash
# Full E2E suite. Use only before claiming the full suite passes.
(cd tests && uv run pytest src/e2e/ -n2 --dist loadscope -v --tb=short)

# One relevant E2E file.
(cd tests && uv run pytest src/e2e/workflows/automation/test_lifecycle.py -v)

# Unit suite, parallel as in CI.
(cd tests && uv run pytest src/unit/ -n auto --tb=short)

# Interactive isolated Home Assistant environment.
uv run hamcp-test-env
uv run hamcp-test-env --no-interactive
```

Locally, `-n2` limits the number of simultaneous Home Assistant containers;
more workers usually add memory pressure without proportional speed. CI uses
`-n3`, a value tuned for CI's 2-vCPU, 15 GB runners. The roughly 11,000 unit
tests take more than 25 minutes serial, so run them in parallel as shown.
The root `pyproject.toml` sets `--maxfail=3`, so
a report with three failures may be an early stop; use `--maxfail=0` only when
the complete failure set is actually needed.

Do not set `HOMEASSISTANT_URL` manually before testcontainer E2E tests.
`tests/.env.test` contains placeholders, and the fixture supplies the real
URL dynamically. The shared token is in `tests/test_constants.py`.

## Code quality

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/ --fix
uv run mypy src/
uv run ast-grep scan
```

C901 complexity is capped at 10 repository-wide with no per-file exemptions;
issue #925 removed the grandfathered list, so extract helpers instead of
adding a `C901` per-file ignore. Lefthook runs `ruff --fix` on commit with
`stage_fixed`; that can remove a newly added but not-yet-used import from
non-`__init__` modules. Add an import and its first use in the same change or
the hook may strip it without a separate Ruff invocation.

Source files are capped at 1,000 lines by
`tests/src/unit/test_module_size_ratchet.py`. Files that were already larger
are listed with their exact line count in
`tests/src/unit/module_size_baseline.json`; a listed file may shrink but not
grow. After shrinking or deleting a listed file, lower its entry and commit
the changed baseline:

```bash
python scripts/module_size_ratchet.py
git add tests/src/unit/module_size_baseline.json
```

The lefthook pre-commit hook runs both steps against the staged content, so
a shrink that is not staged does not lower its entry.

The command lowers or drops entries and never raises or adds one, so a file
that grew has to be split. Vendored trees, test fixtures and the stable
webhook-proxy copy are not counted.

`BLE001` (a handler that catches `Exception` without re-raising it or logging
its traceback) is enabled. Each handler that existed before carries
`# noqa: BLE001`. Do not add that comment to a new handler: catch the specific
exception, re-raise, or log with `logger.exception(...)` or `exc_info=True`.
One case may add it: a tool-boundary handler that ends in
`exception_to_structured_error(exc, ...)` or
`raise_tool_error(create_error_response(...))`. Those calls raise a
`ToolError` for the agent, which ruff cannot see. With `raise_error=False`,
`exception_to_structured_error` returns the payload instead and the handler
needs a real fix.
Logging only the message does not satisfy the rule. `RUF100` fails a `noqa`
that is no longer needed. The two webhook-proxy `start.py` files are exempt
through `per-file-ignores` in `pyproject.toml` and carry no `noqa`.

`PLR0915` caps a function at 50 statements. Functions that were already longer
carry `# noqa: PLR0915` on their `def` line. Do not add that comment to a new
function: extract helpers. `RUF100` fails the `noqa` once the function is short
enough.

## Docker

Stdio mode is local to the process and does not expose a network port:

```bash
docker run --rm -i \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  ghcr.io/homeassistant-ai/ha-mcp:latest
```

For a same-host HTTP client, bind only loopback:

```bash
docker run -d -p 127.0.0.1:8086:8086 \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```
Connect a same-host client to `http://127.0.0.1:8086/mcp`; the default
`MCP_SECRET_PATH` supplies that `/mcp` suffix.

A LAN-reachable listener needs an unguessable MCP path configured in both the
server and client:

```bash
MCP_SECRET="/private_$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
echo "MCP_SECRET_PATH=$MCP_SECRET"
docker run -d -p 8086:8086 \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  -e MCP_SECRET_PATH="$MCP_SECRET" \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```

Read [`SECURITY.md`](../../SECURITY.md) before changing authentication,
network binding, ingress, or proxy behavior.

## Architecture

The stable architectural map is:

```text
src/ha_mcp/
├── server.py                    FastMCP server and lifecycle
├── __main__.py                  CLI and transport entrypoints
├── config.py                    Settings
├── errors.py                    Structured error contract
├── client/                      REST and WebSocket clients
├── auth/                        OAuth provider and consent UI
├── tools/
│   ├── registry.py              Lazy tool discovery
│   ├── tools_*.py               Domain tool modules
│   ├── smart_search/            Search service layer
│   ├── device_control.py        Verified device control
│   └── util_helpers.py          Shared tool utilities
├── settings_ui/                 Web settings interface
├── transforms/                  Shared tool categorization/transforms
├── utils/                       Data paths, sandboxing, hashing, telemetry
└── resources/                   Bundled runtime resources
```

Use the live tree for filenames and counts; this map describes responsibilities,
not an exhaustive inventory.
Concrete owners worth preserving in the map are
`client/websocket_listener.py`, `tools/best_practice_checker.py`,
`utils/fuzzy_search.py`, `utils/operation_manager.py`,
`utils/skill_loader.py`, `utils/python_sandbox.py`, and
`utils/kill_signal_diagnostics.py`.
`utils/config_hash.py` is the shared optimistic-locking implementation for
automation, script, scene, dashboard, and energy configuration.

Registry label updates follow the shared [concurrency strategy](../registry-label-concurrency.md).

Key patterns:

- The registry discovers `tools_*.py` modules and `register_*_tools()`
  functions; adding a module needs no manual central registration.
- Server, client, and tools initialize lazily.
- Shared business logic belongs in service modules such as `smart_search/`
  and `device_control.py`, not duplicated across tool wrappers.
- State-changing device operations verify the result through WebSocket state.
- Tools wait for logical completion when a completion signal exists.

The canonical MCP coding rules are in
[`.gemini/styleguide.md`](../../.gemini/styleguide.md).

## Terminology: apps, not add-ons

Home Assistant 2026.2 renamed add-ons to apps. In user- or agent-facing text,
write **app (add-on)** on first mention and **app** afterwards. Do not use the
retired term alone.

Identifiers are not automatically exempt. Check the current upstream contract:

- The current container prefix is `app_`; `addon_` remains a legacy fallback.
  `tests/src/haos_runtime.py` discovers the live name from Docker instead of
  assuming either prefix.
- The established REST surface remains under `/addons/...`.
- `/v2/apps` is mounted only when the `supervisor_v2_api` feature flag is
  enabled. It was added in 2026-04 with v1 retained for compatibility, is
  absent from older/default Supervisors, and returns `apps` where v1 returns
  `addons`.
- The developer documentation redirects from `/docs/add-ons` to `/docs/apps`.

Old spelling remains correct for app slugs, the `addon` issue label,
`homeassistant-addon*/` paths, the internal `deployment_mode="addon"`
value, and literal historical UI labels in compatibility notes.

## API research

Search Home Assistant Core without cloning the 500 MB+ repository. Use
`gh search code` or the contents API:

```bash
gh search code "use_blueprint" --repo home-assistant/core \
  path:tests --json path --limit 10

gh api /repos/home-assistant/core/contents/homeassistant/components/automation/config.py \
  --jq '.content' | base64 -d
```

Record the upstream revision when a conclusion is version-sensitive.
