"""Environment fixtures shared by the dev-tool tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_dev import FEATURE_FLAG
from ha_mcp.utils.data_paths import get_data_dir

POLICY_ACCESS_FLAG = "HAMCP_DEV_SECURITY_POLICY_ACCESS"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the data dir and the Settings singleton per test."""
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(FEATURE_FLAG, raising=False)
    monkeypatch.delenv(POLICY_ACCESS_FLAG, raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    get_data_dir.cache_clear()
    reset_global_settings()


@pytest.fixture
def _policy_access_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant the dev tools security-policy access (#2141)."""
    monkeypatch.setenv(POLICY_ACCESS_FLAG, "true")
    reset_global_settings()
