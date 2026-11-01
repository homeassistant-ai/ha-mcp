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
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from ha_mcp.tools.component_api import ComponentCaps

# Core's generated flow registry, which the component imports at module level
# for its flow-helper set. Home Assistant is not a unit-test dependency, and the
# module must exist before any test module imports the component. The helper
# list is Core 2026.10's (refresh it from homeassistant/generated/config_flows.py);
# bayesian is a Core integration of type "service", so Core files it under
# "integration".
sys.modules.setdefault(
    "homeassistant.generated.config_flows",
    SimpleNamespace(
        FLOWS={
            "helper": [
                "derivative",
                "filter",
                "generic_hygrostat",
                "generic_thermostat",
                "group",
                "history_stats",
                "integration",
                "min_max",
                "mold_indicator",
                "otp",
                "random",
                "statistics",
                "switch_as_x",
                "template",
                "threshold",
                "tod",
                "trend",
                "utility_meter",
            ],
            "integration": ["bayesian"],
        }
    ),
)

# Core's logbook/recorder helpers are imported at module scope by the
# component's ``ha_mcp_tools/logbook_events`` command. Home Assistant is not a
# unit-test dependency, so the modules must exist before ANY test module
# imports the component surface (same reason as the flow registry above).
for _ha_logbook_stub in (
    "homeassistant.components.logbook",
    "homeassistant.components.logbook.helpers",
    "homeassistant.components.logbook.processor",
    "homeassistant.components.recorder",
    "homeassistant.components.websocket_api",
    "homeassistant.helpers.json",
    "homeassistant.util",
    "homeassistant.util.dt",
):
    sys.modules.setdefault(_ha_logbook_stub, MagicMock())


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


# The constants the ``ha_mcp_tools`` component imports from
# ``homeassistant.const`` function-locally, with Home Assistant's values.
_HA_CONST_DEFAULTS = {
    "EVENT_STATE_CHANGED": "state_changed",
    "ENTITY_MATCH_NONE": "none",
    "ENTITY_MATCH_ALL": "all",
}


@pytest.fixture(autouse=True)
def _ensure_ha_const_names():
    """Guarantee the ``homeassistant.const`` names the component imports.

    The ``call_service`` confirmation-waiter imports ``EVENT_STATE_CHANGED`` and
    ``reference_data`` imports ``ENTITY_MATCH_NONE`` / ``ENTITY_MATCH_ALL`` from
    ``homeassistant.const`` function-locally. Home Assistant is not a unit-test
    dependency, so that submodule is whatever a test module stubbed — and under
    full-suite collection an earlier module can install a ``homeassistant.const``
    stub lacking those names, making the import raise (component tests + the
    write-path contract/routing tests then fail only in the full suite, never in
    isolation). This adds the missing constants — always present in real HA —
    onto whatever stub is live, preserving its other attributes; it never
    replaces a populated stub.
    """
    import sys
    from types import SimpleNamespace

    mod = sys.modules.get("homeassistant.const")
    if mod is None:
        sys.modules["homeassistant.const"] = SimpleNamespace(**_HA_CONST_DEFAULTS)
    else:
        try:
            for name, value in _HA_CONST_DEFAULTS.items():
                if not getattr(mod, name, None):
                    setattr(mod, name, value)
        except (AttributeError, TypeError):
            sys.modules["homeassistant.const"] = SimpleNamespace(**_HA_CONST_DEFAULTS)
    yield


@pytest.fixture(autouse=True)
def _core_helper_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the component's Core loader call with the stubbed helper flow list.

    ``helpers_list`` and ``search`` ask ``homeassistant.loader`` for the helper
    flows in their async pre-steps. The loader is a ``MagicMock`` stub in unit
    tests, which cannot be awaited, so this answers with the ``FLOWS["helper"]``
    stub above, i.e. no custom helper integrations. It does nothing until the
    component is loaded; in a test that loads it later the await fails and the
    pre-step degrades to Core's built-in list with ``helper_flows_degraded``.
    A test that needs custom helper integrations patches the name again.
    """
    from unittest.mock import AsyncMock

    mod = sys.modules.get("custom_components.ha_mcp_tools.websocket_api.flow_domains")
    if mod is None:
        return
    flows = sys.modules["homeassistant.generated.config_flows"].FLOWS["helper"]
    monkeypatch.setattr(
        mod, "async_get_config_flows", AsyncMock(return_value=set(flows))
    )


@pytest.fixture(autouse=True)
def _server_helper_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the server's ``flow_handlers?type=helper`` read with the stubbed list.

    The helper tools read Core's helper flows at call time
    (``helper_flows.helper_flow_types``) through the REST client, which unit
    tests mock per test. This answers with the ``FLOWS["helper"]`` stub above and
    starts every test with an empty per-client cache. A test that needs another
    list patches ``_fetch_helper_flow_types`` again.
    """
    from unittest.mock import AsyncMock

    from ha_mcp.tools import helper_flows

    flows = sys.modules["homeassistant.generated.config_flows"].FLOWS["helper"]
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset(flows)),
    )
    helper_flows._CACHE.clear()


@pytest.fixture
def real_probatio():
    """The installed probatio, past the stub this tier installs for it.

    ``_embedded_stubs`` replaces the module unconditionally with a two-line
    fake, so a plain import in a test would assert against that fake instead
    of the library Core actually converts with. Shared, because both the
    component's normalisation tests and the registry-wide schema guard need
    the real codec.
    """
    import importlib
    import sys

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "probatio" or name.startswith("probatio.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        module = importlib.import_module("probatio")
        assert hasattr(module, "to_openapi"), "got the stub, not the library"
        yield module
    finally:
        for name in [
            name
            for name in sys.modules
            if name == "probatio" or name.startswith("probatio.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)


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
