"""Keep the UAT tooling tests away from the developer's ``~/.ha-mcp``.

Importing ``ha_mcp`` creates its data directory. Test modules import it at
collection time and some tests start the agent script as a subprocess, so the
directory is redirected for the whole session, before any test module loads.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ``tests/uat`` is not a package, so ``uat.*`` imports need ``tests/`` on the
# path; set it here so a single test file collects on its own.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DATA_DIR = tempfile.mkdtemp(prefix="ha-mcp-uat-")


def pytest_configure(config: pytest.Config) -> None:
    os.environ["HA_MCP_CONFIG_DIR"] = _DATA_DIR


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _restore_uat_logger():
    """Undo ``configure_cli_logging()`` after a test that runs an entry point.

    It stops the ``uat`` logger propagating to the root logger, where
    ``caplog`` listens, so every later test in the worker would see no records.
    """
    uat_logger = logging.getLogger("uat")
    saved = (uat_logger.level, uat_logger.propagate, list(uat_logger.handlers))
    yield
    level, uat_logger.propagate, uat_logger.handlers[:] = saved
    uat_logger.setLevel(level)
