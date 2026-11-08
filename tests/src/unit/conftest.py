"""Unit-test environment: no developer state, no side processes.

See ``pytest_configure`` for collection and ``_unit_test_env`` for the
variables every unit test starts with.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from ha_mcp.tools.component_api import ComponentCaps

# Core's own test plugin: the ``hass`` fixture, ``MockConfigEntry`` and its
# socket/thread/task checks. pyproject.toml blocks its entry point so the E2E
# tree never loads it. Nothing above imports Home Assistant: the plugin patches
# Core's JSON and time helpers before Core is first imported.
pytest_plugins = ["pytest_homeassistant.plugins"]

# quickjs starts its shared worker thread on import; importing it here starts
# that thread before any test, so Core's leftover-thread check never sees it.
import quickjs  # noqa: E402


@pytest.fixture
def logbook_component_caps() -> Iterator[ComponentCaps]:
    """Pretend the component advertises ``logbook_events`` (transport tests)."""
    from ha_mcp.tools.component_api import ComponentCaps

    caps = ComponentCaps(
        schema_version=1,
        component_version="2.2.3",
        capabilities=frozenset({"logbook_events"}),
        limits={},
    )
    with patch(
        "ha_mcp.tools.log_sources_logbook.get_component_caps",
        AsyncMock(return_value=caps),
    ):
        yield caps


_ISOLATION_VARS = ("HA_MCP_CONFIG_DIR", "HA_MCP_DISABLE_SETTINGS_UI")
_SESSION_DATA_DIR = ""
_PREVIOUS_ENV: dict[str, str | None] = {}


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


def pytest_collection_finish(session: pytest.Session) -> None:
    for name, value in _PREVIOUS_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def pytest_unconfigure(config: pytest.Config) -> None:
    if _SESSION_DATA_DIR:
        shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _unit_test_env(tmp_path_factory, monkeypatch):
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
    the previous test. ``monkeypatch`` restores all of it after each test, so
    tests from other trees in the same session keep their own environment.
    A test that needs another value sets it with ``monkeypatch``.
    """
    monkeypatch.setenv(
        "HA_MCP_CONFIG_DIR", str(tmp_path_factory.mktemp("ha-mcp-config"))
    )
    monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "false")
    monkeypatch.setenv("HA_MCP_DISABLE_UPDATE_CHECK", "1")
    for name in [var for var in os.environ if var.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    try:
        from ha_mcp.config import _reset_global_settings
        from ha_mcp.utils.data_paths import get_data_dir
    except ImportError:
        # ha_mcp not importable in this test run; nothing to clear.
        yield
        return
    get_data_dir.cache_clear()
    _reset_global_settings()
    yield
    get_data_dir.cache_clear()
    _reset_global_settings()


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
    with _js_harness._worker_lock:
        _js_harness._harness_worker()


@pytest.fixture(autouse=True)
def _no_threads_left_by_the_test() -> Iterator[None]:
    """Stop the per-process threads a test started, before Core's plugin checks.

    A test that kills the JS harness's node worker starts a new one; a usage
    log or a quickjs engine with its own executor (card definitions) opened in
    the test runs its own thread.
    Stopping them here also keeps the next test's usage log in its own config
    directory.
    """
    from . import _js_harness

    worker = _js_harness._worker
    engines: list[Any] = []
    real_function = quickjs.Function

    def recording_function(*args: Any, **kwargs: Any) -> Any:
        engine = real_function(*args, **kwargs)
        if kwargs.get("own_executor"):
            engines.append(engine)
        return engine

    quickjs.Function = recording_function
    try:
        yield
    finally:
        quickjs.Function = real_function
        for engine in engines:
            engine._threadpool.shutdown(wait=True)
        if _js_harness._worker is not worker:
            _js_harness._stop_worker()
        try:
            from ha_mcp.utils.usage_logger import shutdown_usage_logger
        except ImportError:
            pass
        else:
            shutdown_usage_logger()


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
    try:
        from ha_mcp.update_check import get_update_info

        get_update_info.cache_clear()
    except ImportError:
        # ha_mcp not importable in this test run; nothing to clear.
        pass
    yield


@pytest.fixture(autouse=True)
def _restore_fastmcp_host_origin_guard():
    """Restore fastmcp's Host/Origin guard env var + setting after each test.

    ``transport_security.ensure_host_origin_guard_default_off`` runs for real in
    server-creation paths some unit tests exercise (e.g. via ``_create_server``)
    and writes ``os.environ`` and the fastmcp settings singleton directly, which
    ``monkeypatch`` cannot revert. Snapshot and restore both so those mutations do
    not leak across tests once fastmcp exposes the guard (>= 3.4.3).
    """
    env_key = "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION"
    attr = "http_host_origin_protection"
    prev_env = os.environ.get(env_key)
    try:
        from ha_mcp._vendor import fastmcp

        settings = getattr(fastmcp, "settings", None)
    except ImportError:
        settings = None
    has_attr = settings is not None and hasattr(settings, attr)
    prev_setting = getattr(settings, attr) if has_attr else None
    yield
    if prev_env is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = prev_env
    if has_attr:
        setattr(settings, attr, prev_setting)


@pytest.fixture(autouse=True)
def _core_helper_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the component's Core loader call with Core's built-in helper flows.

    ``helpers_list`` and ``search`` ask ``homeassistant.loader`` for the helper
    flows in their async pre-steps, which needs a set-up ``hass``. Tests that
    hand the commands a fake ``hass`` get Core's own ``FLOWS["helper"]``, i.e.
    no custom helper integrations. It does nothing until the component is
    loaded. A test that needs custom helper integrations patches the name again.
    """
    mod = sys.modules.get("custom_components.ha_mcp_tools.websocket_api.flow_domains")
    if mod is None:
        return
    from ._core_helper_flows import CORE_HELPER_FLOWS

    monkeypatch.setattr(
        mod, "async_get_config_flows", AsyncMock(return_value=set(CORE_HELPER_FLOWS))
    )


@pytest.fixture(autouse=True)
def _server_helper_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the server's ``flow_handlers?type=helper`` read with Core's list.

    The helper tools read Core's helper flows at call time
    (``helper_flows.helper_flow_types``) through the REST client, which unit
    tests mock per test. This answers with Core's own ``FLOWS["helper"]`` and
    starts every test with an empty per-client cache. A test that needs another
    list patches ``_fetch_helper_flow_types`` again.
    """
    from ha_mcp.tools import helper_flows

    from ._core_helper_flows import CORE_HELPER_FLOWS

    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=CORE_HELPER_FLOWS),
    )
    monkeypatch.setattr(
        helper_flows,
        "_fetch_core_helper_domains",
        AsyncMock(return_value=CORE_HELPER_FLOWS),
        raising=False,
    )
    helper_flows._CACHE.clear()
    getattr(helper_flows, "_CORE_CACHE", {}).clear()


@pytest.fixture
def tool_search_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the search catalog on the main endpoint (``ENABLE_TOOL_SEARCH``)."""
    from ha_mcp.config import get_global_settings

    monkeypatch.setattr(get_global_settings(), "enable_tool_search", True)


@pytest.fixture
def mock_client():
    """HomeAssistantClient with stubbed internals — no real network.

    Mirror every instance attribute the real ``__init__`` sets, so a
    `getattr` fallback in production code isn't needed to paper over a
    test-fixture omission (and so future `__init__` attribute additions
    that this fixture forgets to mirror fail loudly here instead of
    silently no-op-ing).
    """
    # Imported here rather than at module scope: conftest imports run before
    # ``pytest_configure`` redirects the config directory, so importing ha_mcp
    # during collection creates ~/.ha-mcp and the CI "no local state" gate
    # fails.
    from ha_mcp.client.rest_client import HomeAssistantClient

    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.verify_ssl = True
        client.httpx_client = MagicMock()
        client._supervised_detected = None
        client._is_admin = True
        client._admin_route_refused = False
        client._admin_route_lock = asyncio.Lock()
        return client


@pytest.fixture(autouse=True)
def opened_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, tuple[str, str | None]]:
    """Start every test with no integration flow opened by this server.

    A test that continues a flow by ``flow_id`` seeds it here, as
    ``drive_flow`` would have when it opened the flow.
    """
    from ha_mcp.tools import integration_flow_steps

    flows: dict[str, tuple[str, str | None]] = {}
    monkeypatch.setattr(integration_flow_steps, "_opened_flows", flows)
    return flows


@pytest.fixture
def redact_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the redact_secrets toggle on for one test."""
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )
