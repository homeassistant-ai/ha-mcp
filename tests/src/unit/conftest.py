"""Unit-test fixtures: default ``enable_auto_backup`` off process-wide.

#1288 flipped the production default to ``True`` so addon users get the
safety net out of the box. That broke ~half a dozen pre-existing unit
tests for label / category / helper / discriminator code that mock the
underlying HA client — with the toggle on, the ``@with_auto_backup``
decorator fires for every wrapped call, then ``maybe_snapshot``'s
fetch path tries ``urlparse(MagicMock_client.base_url)`` and trips
``TypeError: '>' not supported between instances of 'MagicMock' and 'int'``.

The right home for the fix is the unit-test process boundary, not each
individual test file: unit tests for unrelated tool code shouldn't have
to know about auto-backup, and a process-wide off default in the unit
tier matches what the tests already assumed implicitly. Tests that DO
want to exercise the auto-backup path (``test_backup_manager.py``,
``test_settings_ui.py``) set ``ENABLE_AUTO_BACKUP=true`` explicitly via
``monkeypatch`` and reset the global settings singleton — that path is
unaffected by this conftest.

The autouse session fixture sets the env var once and resets the
``ha_mcp.config`` singleton so the next ``get_global_settings()`` call
sees the off value. Per-test monkeypatching overrides this for the
specific tests that need it.
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
        pass
    yield
    if previous is None:
        os.environ.pop("ENABLE_AUTO_BACKUP", None)
    else:
        os.environ["ENABLE_AUTO_BACKUP"] = previous
