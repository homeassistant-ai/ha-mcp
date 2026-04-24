"""Unit tests for ha_mcp._version and the __main__ startup log helper."""

from __future__ import annotations

import importlib.metadata
import logging

import pytest

from ha_mcp._version import get_version, is_dev_version, is_running_in_addon


class TestGetVersion:
    """Tests for the version resolution helper."""

    def test_env_override_wins_over_package_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HA_MCP_BUILD_VERSION must take priority so Docker/add-on dev builds
        can surface their dev suffix even though pyproject.toml isn't rewritten."""
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0.dev999")
        assert get_version() == "7.3.0.dev999"

    def test_falls_back_to_ha_mcp_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the env var, resolve via installed package metadata."""
        monkeypatch.delenv("HA_MCP_BUILD_VERSION", raising=False)
        expected = importlib.metadata.version("ha-mcp")
        assert get_version() == expected

    def test_falls_back_to_ha_mcp_dev_when_ha_mcp_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PyPI dev channel installs register as ``ha-mcp-dev`` (renamed package).

        Simulate ``ha-mcp`` not being installed so the fallback loop must try
        ``ha-mcp-dev`` next.
        """
        monkeypatch.delenv("HA_MCP_BUILD_VERSION", raising=False)

        real_version = importlib.metadata.version

        def fake_version(pkg_name: str) -> str:
            if pkg_name == "ha-mcp":
                raise importlib.metadata.PackageNotFoundError(pkg_name)
            if pkg_name == "ha-mcp-dev":
                return "7.3.0.dev42"
            return real_version(pkg_name)

        monkeypatch.setattr(importlib.metadata, "version", fake_version)
        assert get_version() == "7.3.0.dev42"

    def test_returns_unknown_when_not_installed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When neither env var nor package metadata resolves, return 'unknown'
        rather than raising — a missing version shouldn't crash startup — but
        emit a WARNING so the broken install is visible in logs."""
        monkeypatch.delenv("HA_MCP_BUILD_VERSION", raising=False)

        def always_missing(pkg_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(pkg_name)

        monkeypatch.setattr(importlib.metadata, "version", always_missing)
        with caplog.at_level(logging.WARNING, logger="ha_mcp._version"):
            result = get_version()
        assert result == "unknown"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "metadata not found" in warnings[0].getMessage()

    def test_empty_env_var_falls_through_to_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty HA_MCP_BUILD_VERSION (stable Docker default from ARG="")
        must not override — otherwise stable builds would report '' as version."""
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "")
        expected = importlib.metadata.version("ha-mcp")
        assert get_version() == expected


class TestIsDevVersion:
    """Tests for PEP 440 dev-suffix detection."""

    @pytest.mark.parametrize(
        "version",
        ["7.3.0.dev390", "7.3.0.dev0", "8.0.0.dev1", "1.2.3.dev100+abc123"],
    )
    def test_detects_dev_suffix(self, version: str) -> None:
        assert is_dev_version(version) is True

    @pytest.mark.parametrize(
        "version",
        ["7.3.0", "7.3.1", "8.0.0", "1.2.3rc1", "1.2.3a1", "1.2.3b1", "unknown"],
    )
    def test_stable_and_pre_release_versions_are_not_dev(self, version: str) -> None:
        assert is_dev_version(version) is False


class TestIsRunningInAddon:
    """Tests for HA add-on environment detection."""

    def test_detects_supervisor_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-token-abc")
        assert is_running_in_addon() is True

    def test_absent_when_no_supervisor_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert is_running_in_addon() is False

    def test_empty_supervisor_token_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against a spurious empty-string env var masquerading as the addon."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "")
        assert is_running_in_addon() is False


class TestLogStartupVersion:
    """Tests for _log_startup_version() — the behavioral payload of the PR.

    Covers the log emission plus the dev-banner gating logic: banner must fire
    on standalone dev installs and must be suppressed under HA Supervisor so
    add-on users don't get noise (they already see the channel in the HAOS UI).
    """

    def _call(self, caplog: pytest.LogCaptureFixture) -> None:
        from ha_mcp.__main__ import _log_startup_version

        # _log_startup_version uses the module-level ``logger`` in __main__.
        with caplog.at_level(logging.INFO, logger="ha_mcp.__main__"):
            _log_startup_version()

    def test_logs_version_at_info(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        self._call(caplog)
        info_messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        assert any("ha-mcp 7.3.0" in msg for msg in info_messages), info_messages

    def test_dev_version_emits_stable_banner_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0.dev42")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        self._call(caplog)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        msg = warnings[0].getMessage()
        assert ":stable" in msg
        assert "dev channel" in msg

    def test_dev_banner_suppressed_under_supervisor_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The banner must NOT fire inside an HA add-on (noisy, redundant with HAOS UI)."""
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0.dev42")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        self._call(caplog)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
        info_messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        # Version line still fires — add-on users should still see what they're running.
        assert any("7.3.0.dev42" in msg for msg in info_messages), info_messages

    def test_stable_version_never_emits_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Guard against an inverted is_dev_version check leaking the banner to stable users."""
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        self._call(caplog)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
