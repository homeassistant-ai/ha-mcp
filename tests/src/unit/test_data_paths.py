"""Unit tests for the shared data-directory resolver.

The resolver is the single source of truth for where ha-mcp writes its
persistent files (tool config, usage logs). These tests pin its priority
order and the fallback behavior added for issue #1125.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ha_mcp.utils.data_paths import _resolve_data_dir, get_data_dir


@pytest.fixture(autouse=True)
def _reset_cache():
    """Force every test to re-resolve from scratch."""
    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


class TestPriorityOrder:
    """HA_MCP_CONFIG_DIR > /data > ~/.ha-mcp > tempdir/ha-mcp."""

    def test_addon_path_when_supervisor_token_set(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        assert get_data_dir() == Path("/data")

    def test_home_path_when_no_supervisor_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = get_data_dir()
        assert result == tmp_path / ".ha-mcp"
        assert result.is_dir()

    def test_ha_mcp_config_dir_overrides_supervisor_token(self, monkeypatch, tmp_path):
        """HA_MCP_CONFIG_DIR takes precedence even in add-on mode.

        Lets add-on users override the default ``/data`` location, and lets
        hardened-Docker users bind-mount a writable volume without depending
        on ``$HOME``.
        """
        custom_dir = tmp_path / "custom"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(custom_dir))
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")  # would normally route to /data
        result = get_data_dir()
        assert result == custom_dir
        assert custom_dir.is_dir()


class TestFallbacks:
    """Falls back to a writable tmpdir when the preferred location fails."""

    def test_falls_back_to_tmpdir_when_home_unwritable(self, monkeypatch, tmp_path):
        """Issue #1125 regression: ``read_only: true`` Docker, or ``HOME=/``.

        ``mkdir(~/.ha-mcp)`` raises ``OSError(EROFS)``; resolver must fall
        through to ``<tempdir>/ha-mcp`` instead of crashing.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "readonly-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == readonly_home / ".ha-mcp":
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()

    def test_ha_mcp_config_dir_unwritable_chains_to_tmpdir(self, monkeypatch, tmp_path):
        """HA_MCP_CONFIG_DIR mkdir failure chains to tmpdir, doesn't return broken path."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        readonly_parent = tmp_path / "readonly-parent"
        readonly_parent.mkdir()
        broken_target = readonly_parent / "cannot-create"
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == broken_target:
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(broken_target))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "unused-home")
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()
        assert not broken_target.exists()


class TestMemoization:
    """The resolver must memoize so warnings emit once at startup."""

    def test_warning_emitted_only_once_via_module_cache(
        self, monkeypatch, tmp_path, caplog
    ):
        """``get_data_dir`` is hit on every settings UI HTTP request and on
        every usage-log write — without the cache the fallback warning
        would spam logs on every UI toggle.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "readonly-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == readonly_home / ".ha-mcp":
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.data_paths"):
            for _ in range(5):
                get_data_dir()

        fallback_warnings = [
            r for r in caplog.records if "Falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 1, (
            f"expected single fallback warning, got {len(fallback_warnings)}"
        )

    def test_resolve_re_runs_when_cache_cleared(self, monkeypatch, tmp_path):
        """``_resolve_data_dir`` (uncached) re-runs on every call.

        Guards against future refactors that move the cache or accidentally
        memoize the inner function.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(first))
        assert _resolve_data_dir() == first

        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(second))
        assert _resolve_data_dir() == second
