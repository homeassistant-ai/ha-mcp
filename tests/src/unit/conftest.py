"""Unit-test fixtures: default ``enable_auto_backup`` off process-wide.

Unit tests for tool code (label, category, helper, discriminator, etc.)
use ``MagicMock`` HA clients. The production default for
``enable_auto_backup`` is ``True``, which means the
``@with_auto_backup`` decorator's pre-write hook fires on every wrapped
call — and the hook eventually calls ``urlparse(client.base_url)``,
which raises ``TypeError`` against a ``MagicMock`` attribute.

To keep tool unit tests free of that coupling, this conftest sets
``ENABLE_AUTO_BACKUP=false`` once at session start and clears the
cached ``Settings`` singleton so subsequent ``get_global_settings()``
calls observe the off value. Tests that want to exercise the
auto-backup path (e.g. ``test_backup_manager.py``,
``test_settings_ui.py``) opt in via ``monkeypatch.setenv``, which
pytest reverts on teardown — those overrides take precedence and
remain self-contained.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _unit_test_default_auto_backup_off():
    """Force ``enable_auto_backup=false`` for the unit-test process.

    Scoped ``session`` so the env var is set once at collection; tests
    that need to flip it on do so via ``monkeypatch.setenv`` inside the
    test, which automatically reverts on teardown.
    """
    previous = os.environ.get("ENABLE_AUTO_BACKUP")
    os.environ["ENABLE_AUTO_BACKUP"] = "false"
    # Clear the cached Settings singleton so the next call picks up the
    # env var. Import inside the fixture to avoid forcing ha_mcp import
    # at conftest load time (a test that doesn't import ha_mcp would
    # otherwise pull it in transitively here).
    try:
        from ha_mcp.config import _reset_global_settings

        _reset_global_settings()
    except ImportError:
        # ha_mcp not importable in this test run; nothing to reset.
        pass
    yield
    if previous is None:
        os.environ.pop("ENABLE_AUTO_BACKUP", None)
    else:
        os.environ["ENABLE_AUTO_BACKUP"] = previous


@pytest.fixture(autouse=True, scope="session")
def _unit_test_disable_update_check():
    """Disable the PyPI self-update check for the unit-test process.

    The status tools and ``_log_startup_version`` call
    ``update_check.get_update_info``, which would otherwise reach out to
    pypi.org during unrelated unit tests (flaky, slow, and network-coupled).
    Set ``HA_MCP_DISABLE_UPDATE_CHECK`` once at session start; the dedicated
    ``test_update_check.py`` / banner tests opt back in via ``monkeypatch``
    (``delenv`` or by patching ``get_update_info``/``get_update_field``
    directly), which reverts on teardown.
    """
    previous = os.environ.get("HA_MCP_DISABLE_UPDATE_CHECK")
    os.environ["HA_MCP_DISABLE_UPDATE_CHECK"] = "1"
    yield
    if previous is None:
        os.environ.pop("HA_MCP_DISABLE_UPDATE_CHECK", None)
    else:
        os.environ["HA_MCP_DISABLE_UPDATE_CHECK"] = previous


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
