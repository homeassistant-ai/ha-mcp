"""Keep the UAT tooling tests away from the developer's ``~/.ha-mcp``.

Importing ``ha_mcp`` creates its data directory. Test modules import it at
collection time and some tests start the agent script as a subprocess, so the
directory is redirected for the whole session, before any test module loads.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix="ha-mcp-uat-")


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("HA_MCP_CONFIG_DIR", _DATA_DIR)


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_DATA_DIR, ignore_errors=True)
