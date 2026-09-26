"""Unit-test environment: no developer state, no side processes.

See ``pytest_configure`` for collection and ``_unit_test_env`` for the
variables every unit test starts with.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

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


@pytest.fixture(autouse=True)
def _ensure_event_state_changed_const():
    """Guarantee ``homeassistant.const.EVENT_STATE_CHANGED`` for every unit test.

    The ``ha_mcp_tools`` component's ``call_service`` confirmation-waiter imports
    ``EVENT_STATE_CHANGED`` from ``homeassistant.const`` function-locally. Home
    Assistant is not a unit-test dependency, so that submodule is whatever a test
    module stubbed — and under full-suite collection an earlier module can install
    a ``homeassistant.const`` stub lacking that name, making the waiter's import
    raise (component tests + the write-path contract/routing tests then fail only
    in the full suite, never in isolation). This adds the one constant — always
    present in real HA — onto whatever stub is live, preserving its other
    attributes; it never replaces a populated stub.
    """
    import sys
    from types import SimpleNamespace

    mod = sys.modules.get("homeassistant.const")
    if mod is None:
        sys.modules["homeassistant.const"] = SimpleNamespace(
            EVENT_STATE_CHANGED="state_changed"
        )
    elif not getattr(mod, "EVENT_STATE_CHANGED", None):
        try:
            mod.EVENT_STATE_CHANGED = "state_changed"
        except (AttributeError, TypeError):
            sys.modules["homeassistant.const"] = SimpleNamespace(
                EVENT_STATE_CHANGED="state_changed"
            )
    yield


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
