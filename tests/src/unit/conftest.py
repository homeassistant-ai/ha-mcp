"""Unit-test environment: no developer state, no side processes.

The one exception is the JS harness worker, started for JSDOM modules (see
``_js_harness_started_before_tests``).

See ``pytest_configure`` for collection and ``_unit_test_env`` for the
variables every unit test starts with.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import logging
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import WebSocketGenerator

# Core's own test plugin: the ``hass`` fixture, ``MockConfigEntry`` and its
# socket/thread/task checks. pyproject.toml blocks its entry point so the E2E
# tree never loads it. Nothing above imports Home Assistant: the plugin patches
# Core's JSON and time helpers before Core is first imported.
# ``_core_client`` holds the native client fixtures shared by the unit tests.
pytest_plugins = [
    "pytest_homeassistant.plugins",
    "tests.core_stand_in_guard",
    "tests.src.unit._core_client",
    "tests.src.unit._settings_script",
]

# quickjs starts its shared worker thread on import; importing it here starts
# that thread before any test, so Core's leftover-thread check never sees it.
importlib.import_module("quickjs")


_ISOLATION_VARS = ("HA_MCP_CONFIG_DIR", "HA_MCP_DISABLE_SETTINGS_UI")
_SESSION_DATA_DIR = ""
_PREVIOUS_ENV: dict[str, str | None] = {}


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Share Home Assistant's asyncio backend with session-scoped async fixtures."""
    return "asyncio"


def pytest_configure(config: pytest.Config) -> None:
    """Keep collection away from ``~/.ha-mcp`` and the settings sidecar.

    This runs before test modules are imported. Some ``ha_mcp`` modules read
    settings at import time, so a fixture would be too late: collection would
    already have read the developer's ``~/.ha-mcp``. The variables are restored
    once collection finishes, so tests from other trees in the same session run
    with the environment they expect; ``_unit_test_env`` sets them again
    for each unit test.
    """
    global _SESSION_DATA_DIR
    _SESSION_DATA_DIR = tempfile.mkdtemp(prefix="ha-mcp-unit-")
    _PREVIOUS_ENV.update({name: os.environ.get(name) for name in _ISOLATION_VARS})
    os.environ["HA_MCP_CONFIG_DIR"] = _SESSION_DATA_DIR
    os.environ["HA_MCP_DISABLE_SETTINGS_UI"] = "1"
    # Core's test plugin turns DEBUG on for a verbose run only; a quiet run
    # would then skip each ``logger.debug`` call and what a captured record
    # keeps alive.
    logging.getLogger().setLevel(logging.DEBUG)


def pytest_collection_finish(session: pytest.Session) -> None:
    for name, value in _PREVIOUS_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Keep this runner's share of the suite (``UNIT_TEST_SHARD``, set in CI)."""
    shard = os.environ.get("UNIT_TEST_SHARD")
    if not shard:
        return
    from ._shards import in_shard

    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (kept if in_shard(item.nodeid, shard) else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def pytest_unconfigure(config: pytest.Config) -> None:
    if _SESSION_DATA_DIR:
        shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _unit_test_env(monkeypatch):
    """Set the environment every unit test starts from.

    - An empty ``HA_MCP_CONFIG_DIR`` per test. Without it, tests write tool
      config, usage logs or OAuth clients to the developer's ``~/.ha-mcp`` and
      see each other's files.
    - ``HA_MCP_DISABLE_SETTINGS_UI=1``: a test that runs ``main()`` for real
      would otherwise spawn a sidecar that outlives the run.
    - ``ENABLE_AUTO_BACKUP=false``: tool tests use ``MagicMock`` clients, and
      the ``@with_auto_backup`` pre-write hook fails on their ``base_url``.
    - ``HA_MCP_DISABLE_UPDATE_CHECK=1``: the status tools would otherwise call
      pypi.org.
    - No inherited ``GIT_*`` variables, and no global or system git config:
      lefthook runs the suite inside a git hook, whose variables would make
      git in ``tmp_path`` act on the contributor's repository (#2907), and a
      global ``commit.gpgsign`` or ``tag.gpgsign`` would break the tests'
      commits and tags. Details: *Git isolation* in ``tests/AGENTS.md``.

    The cached ``Settings`` is reset too, since it holds the values read for
    the previous test, and so is what an entry point a test ran left behind. ``monkeypatch`` restores all of it after each test, so
    tests from other trees in the same session keep their own environment.
    A test that needs another value sets it with ``monkeypatch``.
    """
    # Not ``tmp_path_factory.mktemp``: pytest numbers a directory by listing
    # every one it made so far, which gets slower with each test of the suite.
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", tempfile.mkdtemp(dir=_SESSION_DATA_DIR))
    monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "false")
    monkeypatch.setenv("HA_MCP_DISABLE_UPDATE_CHECK", "1")
    for name in [var for var in os.environ if var.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    from ha_mcp.config import reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    get_data_dir.cache_clear()
    reset_global_settings()
    # A test that ran an entry point leaves its server and settings mount behind.
    # Only the modules loaded now: a test may still have stand-ins in their place.
    for module, reset in (
        ("ha_mcp.__main__", "reset_main_runtime"),
        ("ha_mcp.settings_ui", "reset_http_settings_state"),
    ):
        clear = getattr(sys.modules.get(module), reset, None)
        if clear is not None:
            clear()
    # A server whose search transform failed marks search unavailable for the
    # whole process; the next test on this worker must not inherit that.
    if (tool_catalog := sys.modules.get("ha_mcp.tool_catalog")) is not None:
        tool_catalog.set_search_available(True)


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core writes into its config directory; a shared one holds other tests' files."""
    return hass_tmp_config_dir


def _uses_js_harness(module: object) -> bool:
    from . import _js_harness

    return any(
        getattr(value, "__module__", None) == _js_harness.__name__
        for value in vars(module).values()
    )


@pytest.fixture(autouse=True, scope="module")
def _js_harness_started_before_tests(request: pytest.FixtureRequest) -> None:
    """Start the JS harness's node worker before a JSDOM test module runs.

    The worker and its two pipe-reader threads live for the whole process
    (see ``_js_harness``). Core's plugin fails a test that ends with a thread it
    did not start with, so the worker starts here, ahead of every test.
    """
    if not _uses_js_harness(request.module):
        return
    from . import _js_harness

    try:
        _js_harness.skip_if_unsupported()
    except pytest.skip.Exception:
        return
    _js_harness.start_worker()


@pytest.fixture(autouse=True)
def _no_threads_left_by_the_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop what a test started on its own, before Core's plugin checks threads.

    That is a replacement JS harness worker (when the test killed the shared
    one), quickjs engines created with ``own_executor`` (card definitions) and
    the usage-log writer. Stopping the writer also keeps the next test's usage
    log in its own config directory. AnyIO stops its worker threads when the
    test's task ends, but does not wait for them to exit; they are joined here.
    """
    from . import _js_harness

    worker = _js_harness.current_worker()
    threads_before = set(threading.enumerate())
    pools: list[concurrent.futures.ThreadPoolExecutor] = []

    pool_type = concurrent.futures.ThreadPoolExecutor
    original_init = pool_type.__init__

    def record_pool(
        pool: concurrent.futures.ThreadPoolExecutor, *args: Any, **kwargs: Any
    ) -> None:
        original_init(pool, *args, **kwargs)
        if str(kwargs.get("thread_name_prefix", "")).startswith("quickjs-worker"):
            pools.append(pool)

    monkeypatch.setattr(pool_type, "__init__", record_pool)
    try:
        yield
    finally:
        for pool in pools:
            pool.shutdown(wait=True)
        if _js_harness.current_worker() is not worker:
            _js_harness.stop_worker()
        from ha_mcp.utils.usage_logger import shutdown_usage_logger

        shutdown_usage_logger()
        for thread in set(threading.enumerate()) - threads_before:
            if thread.name == "AnyIO worker thread":
                thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _logging_put_back() -> Iterator[None]:
    """The CLI entry points (``main_oauth``, ``main_oidc``) reconfigure the root
    logger and set the ``ha_mcp`` and ``fastmcp`` levels process-wide; a later
    test on the same worker would lose its DEBUG records."""
    root = logging.getLogger()
    names = ("ha_mcp", "ha_mcp.auth", "ha_mcp.auth.provider", "fastmcp")
    levels = {name: logging.getLogger(name).level for name in names}
    root_level, root_handlers = root.level, root.handlers[:]
    yield
    # Only what a test changed: ``setLevel`` clears every logger's level cache.
    if root.level != root_level:
        root.setLevel(root_level)
    if root.handlers != root_handlers:
        root.handlers[:] = root_handlers
    for name, level in levels.items():
        if logging.getLogger(name).level != level:
            logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def _ssl_without_truststore_injection() -> None:
    """Undo the ``truststore.inject_into_ssl()`` that importing ``ha_mcp.__main__`` runs.

    The CLI injects truststore into ``ssl`` process-wide; the in-process server
    never imports ``__main__`` for exactly this reason. Core replaces
    truststore's context with a certifi one, and with both in place every new
    ``ssl.SSLContext`` recurses. Test modules import ``__main__`` during
    collection, so each test starts from the stock ``ssl`` module.
    """
    import truststore

    truststore.extract_from_ssl()


@pytest.fixture(autouse=True)
def _clear_update_check_memo():
    """Clear ``get_update_info``'s in-memory ``lru_cache`` before each test.

    ``get_update_info`` memoizes its result process-wide (the check runs once per
    process, no disk). Without clearing, a result memoized by ``test_update_check``
    (which opts back into the check) would leak into unrelated tests in the same
    process. Cleared before each test so every test starts from a cold memo.
    """
    from ha_mcp.update_check import get_update_info

    get_update_info.cache_clear()
    yield


@pytest.fixture(autouse=True)
def _restore_fastmcp_host_origin_guard():
    """Restore fastmcp's Host/Origin guard env var + setting after each test.

    ``transport_security.ensure_host_origin_guard_default_off`` runs for real in
    server-creation paths some unit tests exercise (e.g. via ``create_server``)
    and writes ``os.environ`` and the fastmcp settings singleton directly, which
    ``monkeypatch`` cannot revert. Snapshot and restore both so those mutations do
    not leak across tests once fastmcp exposes the guard (>= 3.4.3).
    """
    env_key = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
    attr = "http_host_origin_protection"
    prev_env = os.environ.get(env_key)
    from ha_mcp._vendor import fastmcp

    settings = getattr(fastmcp, "settings", None)
    has_attr = settings is not None and hasattr(settings, attr)
    prev_setting = getattr(settings, attr) if has_attr else None
    yield
    if prev_env is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = prev_env
    if has_attr:
        setattr(settings, attr, prev_setting)


@pytest.fixture
def tool_search_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the search catalog on the main endpoint (``ENABLE_TOOL_SEARCH``)."""
    from ha_mcp.config import get_global_settings

    monkeypatch.setattr(get_global_settings(), "enable_tool_search", True)


@pytest.fixture(autouse=True)
def _no_opened_flows() -> Iterator[None]:
    """Every test starts and ends with no integration flow opened by this server."""
    from ha_mcp.tools.integration_flow_steps import reset_opened_flows

    reset_opened_flows()
    yield
    reset_opened_flows()


@pytest.fixture(autouse=True)
def _closed_websockets_end_their_keepalive_threads() -> Iterator[None]:
    """``websockets`` closes a synchronous connection without joining its
    keepalive thread, which ends a moment later; Core's teardown check fails a
    test on a thread still alive."""
    before = set(threading.enumerate())
    yield
    for thread in set(threading.enumerate()) - before:
        if thread.name.endswith("(keepalive)"):
            thread.join(5)


@pytest.fixture
def redact_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the redact_secrets toggle on for one test."""
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )


@pytest.fixture
async def component_ws(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> Any:
    """A WebSocket client of a real ``hass`` with the ``ha_mcp_tools/*`` commands.

    The caller is an admin; ``component_ws_read_only`` is the same for a
    non-admin. Use ``await ws_call(client, "ha_mcp_tools/info")`` from
    ``_component_core`` to send one command and get its reply.
    """
    from custom_components.ha_mcp_tools import websocket_api

    websocket_api.async_register_commands(hass)
    return await hass_ws_client(hass)


@pytest.fixture
async def component_ws_read_only(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> Any:
    """``component_ws`` for a user who is not an admin."""
    from custom_components.ha_mcp_tools import websocket_api

    websocket_api.async_register_commands(hass)
    return await hass_ws_client(hass, hass_read_only_access_token)
