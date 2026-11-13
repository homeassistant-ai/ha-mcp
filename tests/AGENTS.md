# Test Infrastructure and Rules

## Custom Component (ha_mcp_tools)

- Component is installed into the Docker container by `_install_custom_component` in `src/e2e/_conftest_seed.py`
- HA's `call_service(return_response=True)` wraps results in `{"changed_states": [], "service_response": {...}}`. Most tools unwrap it with `unwrap_service_response()` (`src/ha_mcp/tools/util_helpers.py`); `ha_call_service` instead *splits* it, projecting `changed_states` into `result` and surfacing `service_response` once at the top level (issue #2085)
- `hass.async_add_executor_job` only passes positional args — use `lambda:` wrappers for calls needing kwargs (e.g., `mkdir(parents=True, exist_ok=True)`)
- HA Docker image uses `annotatedyaml` (PyYAML wrapper), NOT `ruamel.yaml` — custom components needing ruamel must declare it in `manifest.json` requirements
- Feature flags (`ENABLE_YAML_CONFIG_EDITING`, `HAMCP_ENABLE_FILESYSTEM_TOOLS`) are set in `ha_container_with_fresh_config` fixture

## Backend Lanes and How to Gate a Test

The suite runs on several backends, and a test that only makes sense on some
of them is gated by a marker rather than a runtime `skip`. The markers and
their exact skip conditions are defined in
`src/e2e/_conftest_collection.py::pytest_collection_modifyitems` — read that docstring
before adding a gate:

| Marker | Runs on |
|---|---|
| `haos_only` | HAOS backends only (`HAOS_TEST_IMAGE_PATH` set). **Auto-applied** to everything under `src/e2e/haos_only/` — no marker needed there |
| `beta_haos_only` | HAOS beta-image lanes where OS/Supervisor/Core version expectations are configured. If any expectation is set, the test runs so partial configuration fails visibly |
| `container_only` | the testcontainer backend only (includes the container-embedded lane) |
| `embedded_only` | the embedded testcontainer backend only (`E2E_BACKEND=embedded`) — the one lane whose session container has ha-mcp installed inside the HA image. Skips everywhere else, including `haos_embedded` |
| `external_only` | anywhere the server-under-test runs IN the pytest process: plain testcontainer and HAOS external. Skips stdio, inaddon, container-embedded and HAOS-embedded, which cannot be reconfigured via test-process env / monkeypatch or reach an in-process mock. The name is historical — it does NOT mean "HAOS external only" |
| `inaddon_only` | HAOS inaddon mode only (`HAOS_TEST_MODE=inaddon`), where `is_running_in_addon()` paths are live |
| `haos_stdio_only` | HAOS stdio mode only (`HAOS_TEST_MODE=stdio`), where the installed `ha-mcp` command is exercised through a real subprocess transport |
| `haos_embedded_only` | HAOS embedded mode only (`HAOS_TEST_MODE=embedded`), where the in-process server shares Home Assistant's process and event loop — for tests whose subject is that shared loop (#2357). Skips every other lane at collection, so no VM boots just to skip |
| `not_on_embedded` / `not_on_haos_embedded` | everywhere except that lane, for tests the lane's own session backend already covers |
| `requires_tools_entry` | every lane EXCEPT the no-tools lanes (`E2E_NO_TOOLS_ENTRY=1`), where the File & YAML Tools entry is absent (#2292) |
| `no_tools_only` | the no-tools lanes only — the mirror of `requires_tools_entry` |
| `requires_component_surface` | every lane that starts with an active component config entry (`src/e2e/utilities/topology.py::component_surface_available`) — skips the two no-tools lanes without an active server entry (plain container, HAOS inaddon, where it is absent or disabled) |

Pick the marker by what the test *needs*, not by where it happens to pass:
`external_only` is about needing an in-process server you can reconfigure,
`inaddon_only` about needing the app (add-on) supervisor context. Read the skip
expressions, not the summary docstring: `external_only` does not mean
"HAOS external only".

A marker-gated test changes the per-lane skip counts that
`tests/src/e2e/basic/test_backend_dispatch_smoke.py` caps. Record the
increments in a new `tests/src/e2e/basic/skip_ceiling/pr<N>.json` fragment
(`{"reason": ..., "deltas": {lane: n}}`) rather than editing
`_SKIP_CEILING_BASELINE`; fragments from different PRs never conflict. Re-pin
the baseline only from a CI observation, deleting the fragments it absorbs.

**Two different things share the `ha_mcp_tools` name — don't conflate them:**

- **The File & YAML Tools entry** (the privileged filesystem and YAML
  services) is set up on every lane by `_install_custom_component` EXCEPT the
  no-tools lanes (`E2E_NO_TOOLS_ENTRY=1`, see below), where that config entry
  is absent and the privileged services never register. A test that
  relies on tools-entry behaviour must carry `requires_tools_entry`;
  everywhere else a test may still rely on
  component-gated behaviour with no marker — e.g. a config entry's
  `unique_id`, which Home Assistant's own API never exposes on any endpoint.
  Consequence for `ha_search`: a query-driven call is served by the component's
  in-process scan, not the legacy REST path, so a test written for legacy-only
  behaviour passes *vacuously*. This holds when `search_types` names
  `"dashboard"`: the component serves the surfaces its search command
  has while the dashboards leg serves that bucket, merged server-side
  (#2289). Components advertising `search_unified` also serve queryless
  listings, location filters, and result windows above 500, so an oversized
  limit does not select the legacy route.
  Test legacy internals directly with the live `ha_client` when the contract
  under test is specific to that implementation (the reference-graph E2E
  does this). A dashboard-only old-schema request still uses its own
  orchestration because no component search surfaces remain after splitting.

- **The in-process "server" config entry** of that same component (#1527) is
  the embedded backend only, seeded separately. That one IS lane-specific.

In production the component is optional (it ships via HACS), so server code
reading component-only data must degrade honestly for installs without it
rather than assume the e2e's always-present case.

## No-tools lanes (`E2E_NO_TOOLS_ENTRY=1`, #2292)

The rest of the suite always has the "HA-MCP File & YAML Tools" config entry.
`E2E_NO_TOOLS_ENTRY=1` runs a lane without that entry. The lanes that keep the
component match the state most real users are in: integration installed,
second entry never added.
It is orthogonal to the backend selectors, so each backend has its own shape:

| Lane | Topology |
|---|---|
| container (`E2E_BACKEND` unset) | **no component at all** — `_install_custom_component` is skipped for `ha_mcp_tools`, so neither files nor entry exist. A user who never installed the integration |
| container `embedded` | **server entry only** — component files are copied with `seed_entry=False` (no tools entry) and the in-process server entry is seeded as usual |
| HAOS `inaddon` | **no component** in effect — `remove_tools_entry_in_qcow2` drops the baked tools entry pre-boot, and the baked server entry stays disabled until a test enables it (`haos_only/test_embedded_server_haos.py` does, on every HAOS lane but `embedded`) |
| HAOS `embedded` | **server entry only** — same pre-boot removal, and the staged (disabled) server entry is deliberately kept |

The staging lives in `_conftest_testcontainer._prepare_testcontainer_config` (container) and
`_conftest_haos._prepare_haos_image` → `haos_runtime.remove_tools_entry_in_qcow2`
(HAOS). The qcow2 edit is offline and per-worker, like the recorder / HACS
refreshers, and raises rather than warning: a silent no-op would leave the
entry in place and the lane would re-test the ordinary topology while
reporting green.

Two markers gate on it, both dispatched from `pytest_collection_modifyitems`:

- `requires_tools_entry` — the test needs the entry, so it skips on these
  lanes. Applied module-wide to the filesystem / YAML suites.
- `no_tools_only` — the test pins what happens WITHOUT the entry, so it skips
  everywhere else. `src/e2e/workflows/filesystem/test_tools_entry_absent.py`
  is the module that carries it.

The server entry also registers the `ha_mcp_tools/*` WebSocket surface
(#2291), so on the two `embedded` shapes the shared
component capabilities answer while the privileged *services* stay gone —
that split is the whole point of the lanes, and `test_tools_entry_absent.py`
asserts both halves.

## Unit Tests Run on Real Home Assistant Core

`tests/src/unit/` and `tests/addon/` import Core from the dev group
(`homeassistant`, pinned with `pytest-homeassistant` to the e2e image's Core)
and run under Core's own test plugin, which their `conftest.py` loads; its
entry point is blocked in `pyproject.toml`, so E2E sessions never load it.
A test never writes its own Home Assistant. A hand-made one asserts against a
guess of what Core does, and such guesses have shipped bugs (#2752):

- No stand-in for `hass`, a registry, a registry entry, a state, a config
  entry, a WebSocket connection, an entity, a store or any other Core object.
  Use the plugin's `hass` fixture, `MockConfigEntry`, the registry fixtures
  (`entity_registry`, `device_registry`, `area_registry`, ...),
  `hass.states.async_set`, `async_setup_component`, and `hass_ws_client`
  (`component_ws` in `src/unit/conftest.py` registers the `ha_mcp_tools/*`
  commands on it).
- No stand-in module: nothing but the imported module in `sys.modules` for
  `homeassistant.*`, `aiohttp`, `voluptuous` or `yarl`.
- Config entries are set up by Core (`hass.config_entries.async_setup`), which
  loads this repository's `custom_components/ha_mcp_tools` like any custom
  integration; `src/unit/_server_entry.py` adds a server entry whose server is
  replaced.
- No hand-written reply. What Core answers over REST or WebSocket (a state, a
  registry row, a command result, an error reply) comes from Core in the test,
  never from a dict typed into the test, a mocked client method or a fake
  client class. A tool test takes `core_client` (`src/unit/_core_client.py`): the
  server's own client connected to the real `hass`. The test sets Core up
  (`hass.states.async_set`, `async_setup_component`, a registry fixture, a
  config entry) and asserts on the tool's result. A WebSocket command and its
  reply come from the integration that owns it, never from a command the test
  registers: where the integration talks to a device or a service, only what
  is below its library is replaced, as Core's own tests replace it (Core's test
  kit for the integration, `aioclient_mock` for the HTTP API it calls). A test
  may re-register a command's own handler with its own schema to observe it,
  to hold it until the test releases it (the handler then runs and answers),
  or to drop the connection; it never answers for it.
  `component_core_client` also registers the `ha_mcp_tools/*` commands. Data
  handed straight to a pure helper follows the same rule: get it from Core.
- What may be replaced: this repository's own collaborators that do not speak
  for Home Assistant (the in-process server, an upstream), another integration
  through Core's `mock_integration`, and one function or constant on a real
  Core module when the test needs an answer Core cannot be made to give in a
  unit test (a failure, a URL, another Core version's shape). Another Core
  version's shape is for the occasional case that needs it, such as a beta
  change with a fallback to current stable or an update path; it is not a
  per-PR check for older releases. Raise Core's
  real exceptions. A client method is replaced for one thing only: the client
  getting no answer at all (a dropped connection, a timeout), as
  `AsyncMock(side_effect=HomeAssistantConnectionError(...))`. An error reply
  (a 404, a rejected command) is an answer, and comes from Core. To prove a
  call was made, counted or not made, spy on the real method
  (`AsyncMock(wraps=client.get_states)`): it calls through, so the reply is
  still Core's.
- An integration a test runs gets its requirements in the dev group at the
  pins of Core's manifest (`src/unit/test_dev_integration_pins.py` keeps them
  equal). One whose requirements are not installed with Core (`cloud`,
  `camera`, `conversation`) cannot be imported here; the component reaches it
  through a small function of its own that the test replaces.

Ten ast-grep rules refuse these in the source: a hand-made `hass`, also as
`mock_hass`, `hass_mock`, `self.hass` or a test file's own `hass` fixture, or a
placeholder put into `hass.data`
(`rules/no-hand-made-hass.yml`), a fake Core object class
(`rules/no-fake-core-objects.yml`), an import of the old shared fakes
(`rules/no-shared-core-fakes.yml`), `patch.dict(sys.modules, ...)` or an
assignment of a Core module name in `sys.modules`
(`rules/no-core-module-stand-ins.yml`), a replaced client method, client or
HTTP transport, or a hand-written client class
(`rules/no-hand-made-core-replies.yml`), an import from another test module or
from the old fake-client helpers, or the `mock_client` fixture, each of which
hands a file someone else's fake client (`rules/no-shared-fake-clients.yml`),
and a comment that switches one of these checks off for a line or a file
(`rules/no-stand-in-suppressions.yml`: `ast-grep-ignore`, `noqa: SLF001`, a
bare `noqa`, `mypy: ignore-errors`, a bare `type: ignore` or one for
`assignment`, `attr-defined` or `misc`). The eighth
(`rules/no-untyped-core-fixtures.yml`) refuses a Core fixture that a test or
fixture takes with no annotation or typed `Any`; see the mypy check below.
The ninth (`rules/no-test-websocket-replies.yml`) refuses a WebSocket command
a test registers (`@websocket_api.websocket_command`) and a reply it writes on
a connection (`connection.send_result`, `send_error`, `send_message`,
`send_event`); it has no list, so every unit test complies. The tenth
(`rules/no-test-registry-store-writes.yml`) refuses a write into a registry's
store (`.issues`, `.entities`, `.deleted_entities`, `.devices`,
`.deleted_devices`, `.areas`, `.floors`, `.labels`, `.categories`): an item
assigned or deleted by key, or `pop`, `clear`, `update` or `setdefault` on
it. The test creates, changes or removes the entry through Core, and gets a
restored one by having Core load it from storage. It has no list either.
`tests/core_stand_in_guard.py` checks `sys.modules` after every test body and
fails a test whose stand-in module is still there, which covers `monkeypatch`
swaps and computed names; it cannot see a swap the test has already undone,
which is why the fourth rule exists.

Test files written before a check are listed with it: in the rule's `ignores`
and in the guard. Converting a file removes it from every list it is on: the
ast-grep `ignores`, the guard, `SLF001` and the mypy list below. A file taken
off one list and left on another fails the ratchet below.
`src/unit/test_core_reply_rules.py` keeps the lists of the hass, reply,
shared-client, suppression and fixture-annotation rules exact: a listed file that no longer
violates fails it, as does a violating file that is not listed.

A new test is never listed. Fast Checks compares every list with the pull
request's base (`scripts/stand_in_lists_ratchet.py`) and fails when a file that
is not in the base appears on one, whatever else the pull request changes. A
file that is in the base may be added only by the pull request that creates the
check or changes what its ast-grep rule matches, because a wider rule matches
older files. The script also fails a rule that is removed, renamed or made to
scan fewer files.

A listed file is exempt for what it already holds, not for what is added to
it. For each file on an ast-grep rule's list or a `SLF001` list that the pull
request changes, the same script counts what the check refuses at the base
and in the pull request, and fails when the count rises. A test added to a listed file goes in
a new file on real Core, or the file is converted.

Two more checks run on these trees, each with a list under the same rules:

- Ruff's `SLF001` (`src/unit/ruff.toml`, `addon/ruff.toml`) refuses private
  member access. Setting a private attribute on a Core object forces a state
  Core never produces, and calling a private function skips the entry point a
  user reaches: go through the public command or tool.
  `test_core_reply_rules.py` keeps these lists exact too. Importing the name
  (`from module import _name`, or any name from a private module such as
  `ha_mcp._internal`) skips the entry point the same way and SLF001 does not
  see it, so the same test fails a file that is on none of these lists and
  still imports one. The vendored packages' import path (`ha_mcp._vendor`) and
  the test trees' own helper modules are exempt; a private name inside a
  vendored package is not. A file still on a
  list loses its private imports when it is converted.
- mypy checks both trees against Core's real types
  (`uv run python scripts/mypy_tests_exact.py`). A `None` or a placeholder
  assigned where Core declares a type is an error wherever mypy knows the
  object's type: an object the test builds, or a fixture the test annotates.
  An unannotated fixture parameter is `Any` and mypy checks nothing done to
  it, so a test annotates the Core fixtures it takes with their real types
  (`hass: HomeAssistant`, `core_client: HomeAssistantClient`); this is the one
  exception to tests needing no type hints. The files written before the check are
  the `ignore_errors` modules under `tests.` in `pyproject.toml`; the script
  runs mypy without that list and fails on an error in an unlisted module and
  on a listed module that has none left.

The plugin enforces, for every test:

- a fresh event loop (the E2E tree keeps one session loop, see
  `src/e2e/_conftest_collection.py`);
- no sockets: a test that runs its own loopback listener requests
  `socket_enabled`, and only `127.0.0.1` accepts connections;
- no thread, task or timer left behind (`_no_threads_left_by_the_test` in
  `src/unit/conftest.py` stops the per-process ones).

## Test Design Rules

Every test under `tests/` follows these. Reviewers check new and changed
tests against them.

1. **Test our code, not the platform.** Assert on behaviour this repository
   owns, not that Python, FastMCP, pydantic or Home Assistant do what their
   docs say. A dependency may set up the failure; the assertion is on how
   our code handles it. See the FastMCP schema-validation note under
   [E2E Test Patterns](#e2e-test-patterns).
2. **Prove the test catches the bug.** Write the regression test first
   and watch it fail, as the root `AGENTS.md` requires. When a test is
   added to a fix that already exists (for example during review), revert
   the fix, confirm the test fails, then restore it. A test that builds its
   input by hand, or covers only a pure helper, can pass with the fix
   reverted.
3. **One property, one test.** Test each property at the lowest layer that
   can see it: logic in unit tests; wiring, packaging and real Home
   Assistant behaviour in E2E. Two tests that assert the same property are
   one too many, whatever layer or client they use. Check for an existing
   test first. In a parametrized table, keep one case per branch, plus any
   value that failed in the wild. If deleting a case leaves coverage
   unchanged, it was a duplicate.
4. **Do not restate the implementation.** Assert the property that must
   hold, not the code's own formula or literals. For example, assert that a
   truncated response is under its size limit, not that its length equals
   the number the code computed. This covers configuration too: a test that
   asserts a value chosen freely (a path, a timeout, a version number) fails
   on a change only because it restates the value, so the revert check in
   rule 2 proves nothing for it. Explain such a value in a comment, or test
   a property that holds across the file, such as every analysis output
   being gated. When something outside the file requires the value, such as
   a ruleset's required check name or a policy that a suite must run,
   assert that requirement.
5. **Make the untestable path testable.** A failure path a test cannot
   reach gets a small, obvious seam the test can replace.
6. **A test supplies its own world.** No reads from the real home directory
   or `~/.ha-mcp`, no network beyond the test's own containers, no answer
   taken from the wall clock, the local timezone or another test's
   leftovers. Another test's leftovers include the registry entry of a
   deleted zone or name-keyed storage helper (`input_*`, `counter`,
   `timer`, `schedule`, `person`): Home Assistant keeps it for 30 days and
   restores its entity_id (if still free), name, icon, area, labels and
   disabled state when an item with the same storage id is created, and
   that id is the slugified name. A test or fixture that creates one gives
   the name a fresh suffix, such as `uuid.uuid4().hex[:8]`. Code that reads
   the clock takes `now` as a parameter, unless the test harness already
   controls the clock (the JSDOM harness below does).
   See **Config-dir isolation** below.
   Installing a pinned dependency during job setup is not test network
   access. Do not commit third-party or generated files to satisfy this
   rule.
7. **Name the defect.** The test name, and its docstring or the
   framework's own description, say what breaks for the user, not the
   function name.

## Test Patterns

- Tests expecting tool **success**: use `mcp.call_tool_success()` inside `MCPAssertions` context
- Tests expecting tool **failure**: use `mcp.call_tool_failure()` inside `MCPAssertions`
  context. It rejects any result `assert_mcp_success()` would accept — including the
  tools that succeed with no `success` key (`pending_restart`, bulk-operation payloads)
  — so it genuinely proves the call failed. Prefer also passing `expected_error` to pin
  *which* failure; about half the current call sites omit it and assert on the returned
  dict themselves instead, which is equally fine.
- `safe_call_tool()` is for calls whose outcome the test does **not** assert: removing
  or aborting, in a `finally` block, something the test created or opened, and
  service-availability probes. It swallows `ToolError` and returns a parsed dict, so using
  it for an expected failure means nothing verifies the call failed at all.
- A `finally` block that puts back state a later test on the worker reads runs its steps
  in `CleanupSteps` (`src/e2e/utilities/cleanup.py`): one `with cleanup.step():` per
  step. A tool call in a step goes through `MCPAssertions(client).call_tool_success()`;
  a helper counts as checked when it raises on failure. Removing or aborting, in the same
  block, something the test created or opened may stay on `safe_call_tool()`, in a step
  of its own. A step that raises an `Exception` or calls `pytest.fail()` or
  `pytest.skip()` is logged, and the next step runs. A failed step fails a test whose
  body passed or was skipped; after a failed body it is only logged, so pytest reports
  the body's own failure. Enter `CleanupSteps` as the first statement of the `finally`
  block, and not in a `try`/`finally` that runs while another exception is handled (an
  `except` in the `finally` block, a caller's `except`) or whose body raises an exception
  the caller expects (`pytest.raises`): it would read that exception as a failed body and
  only log a failed step. A restore that must never fail the test keeps its own
  `try`/`except` and a comment saying why.

## E2E Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**Automation configs come back as Core stores them:** the automation tools send the config to Core as written and `ha_config_get_automation` returns Core's stored body verbatim, so `python_transform` operates on that body. Core itself renames the singular root keys `trigger`/`condition`/`action` to the plural forms and `service` to `action` while validating a write (Core's backward-compat validators: `cv.renamed` for the root keys, the service schema's own rename for `service`; both rename in place and the config view stores that dict), and refuses a config holding both forms of one key; a trigger's `platform` key is kept. So a config written with singular keys reads back plural, and `test_data_factory.automation_config`'s singular `trigger`/`action` defaults must be overridden with the same singular keys, never the plural ones.
An automation the registry knows but Core's config API answers "Resource not found" for (defined in YAML or a package) is CONFIG_NOT_FOUND on get, set and remove. The component's lookup reports `loaded`; while Core has not loaded the automation, `references` is None and the get tool warns. The reference validator reads Core's verdict from the component's `config_references`; without the component it reports `validation.references_unchecked`.

**Poll after creating entities.** After creating an entity (automation, script, helper, etc.), HA needs time to register it. Never search/query immediately — use polling helpers from `tests/src/e2e/utilities/wait_helpers.py`:
```python
from ..utilities.wait_helpers import wait_for_tool_result

data = await wait_for_tool_result(
    mcp_client,
    tool_name="ha_search",
    arguments={"query": "my_sensor", "search_types": ["automation"], "limit": 10},
    predicate=lambda d: len(d.get("automations", [])) > 0,
    description="ha_search finds new automation",
)
```
Other available helpers: `wait_for_entity_state()`, `wait_for_condition()`, `wait_for_state_change()`. See `wait_helpers.py` for the full set.

**Exception handling in polling helpers.** `wait_helpers.py` catches a narrow `_POLLING_TRANSIENT_ERRORS` tuple inside retry loops; bugs like `TypeError` / `AttributeError` / `KeyError` / `AssertionError` propagate immediately. Don't broaden to `except Exception`.

## JS Behaviour Testing (`tests/js/`, `tests/src/unit/_js_harness.py`)

Every rendered `<script>` body in the repo (`src/ha_mcp/settings_ui/` — page
HTML in `settings.html`, client JS in `settings_js/`;
`src/ha_mcp/auth/consent_form.py`; every `.astro` page under `site/src/`)
gets parse coverage automatically via
`tests/src/unit/test_rendered_scripts_parse.py`. The discovery walker in
`_js_harness.py::discover_script_surfaces` picks up new surfaces on its
next run — no registration needed when you add a new UI.

For behavioural tests, use the JSDOM harness:

```python
from ._js_harness import extract_script_body, run_script

script = extract_script_body(rendered_html)
result = run_script(
    script,
    initial_html="<!DOCTYPE html>...",
    fetch_map={"/api/foo": {"status": 200, "json": {...}}},
    broadcast_events=[{"channel": "ch-name", "data": {"type": "..."}}],
    invoke="await window.someExposedFn();",
)
assert result.reloads == 1
assert result.broadcasts_of_type("restart-required")
```

The harness fakes `setTimeout` / `setInterval` / `Date.now` on a virtual clock, stubs `fetch` from a URL map, captures `location.reload` via JSDOM's `jsdomError` channel, and provides a `BroadcastChannel` shim. `new Date()` / `performance.now()` continue to report wall time — only the three sources above are faked.

Each pytest worker sends all its runs to one long-lived node process, and every run gets a fresh JSDOM window. A run that times out, crashes node or garbles its reply ends that process; the next run starts a new one. `tests/src/unit/test_js_harness_worker.py` covers this.

Astro `<script>` blocks without `define:vars` / `is:inline` are TypeScript by default — pass `language="ts"` to `run_script`. A page whose `<script>` imports modules (the setup wizard) is bundled first with `bundle_inline_script`, which resolves the imports from the page's directory and returns plain JS; `module_exports` reads a module's exported data (the wizard's `data.ts`) so tests use production data instead of a copy.

CI installs Node + jsdom in the `unit-tests` job. Local devs without `tests/js/node_modules/` get clean skips. Each harness run has a 15s node timeout; set `HA_MCP_JS_HARNESS_TIMEOUT=<seconds>` to raise it on slower hardware.

**Transient UI + the fake clock:** timed UI (e.g. the save toast, ~4s auto-dismiss) is gone from `result.dom` by capture time because the virtual clock fast-forwards. Stamp state into a `data-` attribute *inside* `invoke` to read it live. Avoid substring false-positives too — `"ha-toast" in result.dom` matches the always-present `#ha-toast-region`; assert the specific variant class.

**Config-dir isolation:** `tests/src/unit/conftest.py` gives every unit test its own empty `HA_MCP_CONFIG_DIR` and sets `HA_MCP_DISABLE_SETTINGS_UI`, so no unit test reads or writes `~/.ha-mcp` or starts a settings sidecar. A sidecar or startup test that needs the sidecar unsets `HA_MCP_DISABLE_SETTINGS_UI` with `monkeypatch`; a test of the default data-dir resolution unsets `HA_MCP_CONFIG_DIR` and fakes `Path.home`. Core writes into its own config directory too (it copies its blueprints there when `automation`, `script` or `template` is set up), so `hass_config_dir` in `tests/src/unit/conftest.py` and `tests/addon/conftest.py` gives every unit and app test a temporary one; a file overrides it only to prepare that directory, and CI fails when Core's shared `tests/testing_config` appears. In E2E, `src/e2e/_collection_data_dir.py` does the same for collection, and the `in_process_data_dir` session fixture points the in-process server's `HA_MCP_CONFIG_DIR` and `HAMCP_BACKUP_DIR` at a temporary directory.

**Git isolation:** `_unit_test_env` in `tests/src/unit/conftest.py` also removes every inherited `GIT_*` variable and sets `GIT_CONFIG_GLOBAL=os.devnull` and `GIT_CONFIG_NOSYSTEM=1`. lefthook runs the unit suite inside a git hook, and the hook's variables point at the contributor's repository: `GIT_DIR` and `GIT_INDEX_FILE` in a linked worktree, an absolute `GIT_INDEX_FILE` for `git commit -a` or `git commit <paths>` in any checkout, and `GIT_CONFIG_PARAMETERS` for `git -c`. The fixture strips the whole prefix because a list of names misses some of them. The contributor's global config (commit signing, identity) must not reach the tests either. Git that runs before that fixture, from a fixture scoped wider than function or at import time, passes an `env=` without any `GIT_*` from `os.environ`, plus those two variables: the module-scoped `changelog` fixture in `test_changelog_template.py` takes only `PATH` from `os.environ`.

When adding a new UI surface:
- Python-rendered HTML: register the renderer in `_js_harness.py::_PY_RENDERERS`.
- Astro page: drop the `.astro` file under `site/src/`; discovery walks automatically.
- Behavioural tests: add a `test_<surface>_js_behavior.py` module alongside the existing ones (`test_settings_ui_js_behavior.py`, `test_astro_setup_js_behavior.py`, `test_astro_tools_js_behavior.py`, `test_astro_layout_js_behavior.py`, `test_consent_form_js_behavior.py`) — one module per UI surface.
