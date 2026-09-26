"""Point ``HA_MCP_CONFIG_DIR`` at a temp dir before ``ha_mcp`` is imported.

Importing ``ha_mcp`` reads settings, which creates the data dir and caches
feature flags from it. pytest imports ``conftest.py`` before any
``pytest_configure`` hook runs, so the conftest imports this module ahead of
``ha_mcp``. The ``in_process_data_dir`` fixture replaces the directory for the
tests themselves.
"""

import atexit
import os
import shutil
import tempfile

COLLECTION_DATA_DIR = tempfile.mkdtemp(prefix="ha-mcp-e2e-")
atexit.register(shutil.rmtree, COLLECTION_DATA_DIR, ignore_errors=True)
os.environ["HA_MCP_CONFIG_DIR"] = COLLECTION_DATA_DIR
