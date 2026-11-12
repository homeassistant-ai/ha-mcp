"""Unit tests for the version ``ha-mcp`` reports and its startup banners."""

from __future__ import annotations

import importlib.metadata
import logging
import sys
from collections.abc import Callable

import pytest

from ._main_web import start_main_web


@pytest.fixture
def reported_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[[], str]:
    """Read the version ``ha-mcp --version`` prints."""
    from ha_mcp.__main__ import main

    def read() -> str:
        monkeypatch.setattr(sys, "argv", ["ha-mcp", "--version"])
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 0
        printed = capsys.readouterr().out.strip()
        assert printed.startswith("ha-mcp ")
        return printed.removeprefix("ha-mcp ")

    return read


def _startup_records(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    version: str,
    supervisor_token: str | None,
) -> list[logging.LogRecord]:
    """What an ``ha-mcp-web`` start of ``version`` logs, loopback-bound so the
    default-path warning stays out of the way."""
    with caplog.at_level(logging.INFO, logger="ha_mcp.__main__"):
        start_main_web(
            monkeypatch,
            HA_MCP_BUILD_VERSION=version,
            SUPERVISOR_TOKEN=supervisor_token,
            MCP_HOST="127.0.0.1",
        )
    return [r for r in caplog.records if r.name == "ha_mcp.__main__"]


def _warnings(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno == logging.WARNING]


class _FakeDist:
    def __init__(self, top_level: str | None, files: list[str]) -> None:
        self._top_level = top_level
        self.files = [importlib.metadata.PackagePath(f) for f in files]

    def read_text(self, name: str) -> str | None:
        return self._top_level if name == "top_level.txt" else None


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch):
    """Replace the installed distributions with the given fakes.

    Each value is ``(version, top_level_txt_or_None, files)``. Reading any
    other distribution, or scanning all of them, fails the test: the tests
    must not depend on the developer's environment.
    """
    monkeypatch.delenv("HA_MCP_BUILD_VERSION", raising=False)

    def use(dists: dict[str, tuple[str, str | None, list[str]]]) -> None:
        def lookup(name: str) -> tuple[str, str | None, list[str]]:
            if name not in dists:
                raise importlib.metadata.PackageNotFoundError(name)
            return dists[name]

        def scan_everything() -> None:
            raise AssertionError("scanned every installed distribution")

        monkeypatch.setattr(importlib.metadata, "version", lambda n: lookup(n)[0])
        monkeypatch.setattr(
            importlib.metadata,
            "distribution",
            lambda n: _FakeDist(lookup(n)[1], lookup(n)[2]),
        )
        monkeypatch.setattr(
            importlib.metadata, "packages_distributions", scan_everything
        )

    return use


_WHEEL_FILES = ["ha_mcp/__init__.py", "ha_mcp/server.py"]


class TestGetVersion:
    """Tests for the version resolution helper."""

    def test_env_override_wins_over_package_metadata(
        self, reported_version: Callable[[], str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HA_MCP_BUILD_VERSION must take priority so Docker/add-on dev builds
        can surface their dev suffix even though pyproject.toml isn't rewritten."""
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.3.0.dev999")
        assert reported_version() == "7.3.0.dev999"

    def test_reads_the_installed_stable_package(
        self, reported_version: Callable[[], str], installed
    ) -> None:
        """Without the env var, the version comes from the package metadata."""
        installed({"ha-mcp": ("8.6.0", None, _WHEEL_FILES)})
        assert reported_version() == "8.6.0"

    def test_reads_the_dev_channel_package_when_stable_is_absent(
        self, reported_version: Callable[[], str], installed
    ) -> None:
        """PyPI dev channel installs register as ``ha-mcp-dev``."""
        installed({"ha-mcp-dev": ("7.3.0.dev42", None, _WHEEL_FILES)})
        assert reported_version() == "7.3.0.dev42"

    def test_returns_unknown_when_not_installed(
        self,
        reported_version: Callable[[], str],
        installed,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When neither env var nor package metadata resolves, return 'unknown'
        rather than raising — a missing version shouldn't crash startup — but
        emit a WARNING so the broken install is visible in logs."""
        installed({})
        with caplog.at_level(logging.WARNING, logger="ha_mcp._version"):
            result = reported_version()
        assert result == "unknown"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "metadata not found" in warnings[0].getMessage()

    def test_empty_env_var_falls_through_to_metadata(
        self,
        reported_version: Callable[[], str],
        installed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty HA_MCP_BUILD_VERSION (stable Docker default from ARG="")
        must not override — otherwise stable builds would report '' as version."""
        installed({"ha-mcp": ("8.6.0", None, _WHEEL_FILES)})
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "")
        assert reported_version() == "8.6.0"


class TestIsDevVersion:
    """PEP 440 dev-suffix detection, seen as the dev-channel startup banner."""

    @pytest.mark.parametrize(
        "version",
        ["7.3.0.dev390", "7.3.0.dev0", "8.0.0.dev1", "1.2.3.dev100+abc123"],
    )
    def test_detects_dev_suffix(
        self,
        version: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        warnings = _warnings(_startup_records(monkeypatch, caplog, version, None))
        assert any("dev channel" in m for m in warnings), warnings

    @pytest.mark.parametrize(
        "version",
        ["7.3.0", "7.3.1", "8.0.0", "1.2.3rc1", "1.2.3a1", "1.2.3b1", "unknown"],
    )
    def test_stable_and_pre_release_versions_are_not_dev(
        self,
        version: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        warnings = _warnings(_startup_records(monkeypatch, caplog, version, None))
        assert not any("dev channel" in m for m in warnings), warnings


class TestAddonStartLogsVersionBanner:
    """The add-on must log the ha-mcp version + update banner at startup."""

    def test_addon_start_invokes_log_startup_version(self) -> None:
        """``homeassistant-addon/start.py`` runs its own startup (it does NOT go
        through ``__main__.main_web``), so it must call ``_log_startup_version()``
        itself — otherwise the ha-mcp version + self-update banner never reach the
        add-on logs (only FastMCP's banner does, via ``run_async``). Regression
        guard for that wiring."""
        from pathlib import Path

        start_py = (
            Path(__file__).resolve().parents[3] / "homeassistant-addon" / "start.py"
        )
        source = start_py.read_text(encoding="utf-8")
        assert "_log_startup_version" in source, (
            "start.py must import _log_startup_version"
        )
        assert "_log_startup_version()" in source, (
            "start.py must call _log_startup_version() during add-on startup"
        )


class TestIsRunningInAddon:
    """HA add-on detection, seen as the dev banner it suppresses."""

    def test_empty_supervisor_token_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Guard against a spurious empty-string env var masquerading as the addon."""
        warnings = _warnings(_startup_records(monkeypatch, caplog, "7.3.0.dev42", ""))
        assert any("dev channel" in m for m in warnings), warnings


class TestLogStartupVersion:
    """The version line and banners an ``ha-mcp-web`` start logs.

    Covers the log emission plus the dev-banner gating logic: banner must fire
    on standalone dev installs and must be suppressed under HA Supervisor so
    add-on users don't get noise (they already see the channel in the HAOS UI).
    The self-update check is off in every unit test (``conftest.py``) unless a
    test replaces it.
    """

    def test_logs_version_at_info(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        records = _startup_records(monkeypatch, caplog, "7.3.0", None)
        info_messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert any("ha-mcp 7.3.0" in msg for msg in info_messages), info_messages

    def test_dev_version_emits_stable_banner_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        warnings = _warnings(_startup_records(monkeypatch, caplog, "7.3.0.dev42", None))
        assert len(warnings) == 1, warnings
        assert ":stable" in warnings[0]
        assert "dev channel" in warnings[0]

    def test_dev_banner_suppressed_under_supervisor_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The banner must NOT fire inside an HA add-on (noisy, redundant with HAOS UI)."""
        records = _startup_records(monkeypatch, caplog, "7.3.0.dev42", "hassio-abc")
        assert _warnings(records) == []
        info_messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
        # Version line still fires — add-on users should still see what they're running.
        assert any("7.3.0.dev42" in msg for msg in info_messages), info_messages

    def test_stable_version_never_emits_dev_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Guard against an inverted dev check leaking the dev-channel banner to
        stable users."""
        assert _warnings(_startup_records(monkeypatch, caplog, "7.3.0", None)) == []

    def test_stable_emits_update_banner_when_newer_available(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A standalone stable install with a newer PyPI release gets an
        update banner (the dev-channel banner stays silent)."""
        from ha_mcp import update_check

        monkeypatch.setattr(
            update_check,
            "get_update_info",
            lambda: update_check.UpdateInfo("7.8.0", "7.9.0", True),
        )
        warnings = _warnings(_startup_records(monkeypatch, caplog, "7.8.0", None))
        assert any("available: 7.9.0" in m for m in warnings), warnings
        assert not any("dev channel" in m for m in warnings), warnings

    def test_update_banner_names_the_python_the_release_needs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The banner passes the release's requires_python on: a pip hint
        would reinstall the old version on a Python the release excludes."""
        from ha_mcp import update_check

        monkeypatch.setattr(update_check, "_running_in_docker", lambda: False)
        monkeypatch.setattr(update_check, "_running_python", lambda: "3.13.7")
        monkeypatch.setattr(
            update_check,
            "get_update_info",
            lambda: update_check.UpdateInfo(
                "9.1.2", "9.2.0", True, requires_python=">=3.14"
            ),
        )
        warnings = _warnings(_startup_records(monkeypatch, caplog, "9.1.2", None))
        assert any("uvx --python 3.14 ha-mcp@latest" in m for m in warnings), warnings
        assert not any("pip install" in m for m in warnings), warnings

    def test_stable_no_update_banner_when_current(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No banner when already on the latest stable."""
        from ha_mcp import update_check

        monkeypatch.setattr(
            update_check,
            "get_update_info",
            lambda: update_check.UpdateInfo("7.9.0", "7.9.0", False),
        )
        assert _warnings(_startup_records(monkeypatch, caplog, "7.9.0", None)) == []

    def test_update_banner_fires_under_supervisor_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The update banner fires in the add-on too (copying FastMCP, which
        announces on every startup regardless of deployment) — an add-on user who
        ignored the Supervisor prompt still sees it in the logs. The add-on
        upgrade hint points at the Supervisor UI, not pip/docker."""
        from ha_mcp import update_check

        monkeypatch.setattr(
            update_check,
            "get_update_info",
            lambda: update_check.UpdateInfo("7.8.0", "7.9.0", True),
        )
        records = _startup_records(monkeypatch, caplog, "7.8.0", "hassio-abc")
        warnings = _warnings(records)
        assert any("available: 7.9.0" in m for m in warnings), warnings
        # add-on upgrade hint, not the dev-channel banner (suppressed in add-on)
        assert any(
            "Settings -> Apps -> Home Assistant MCP Server" in m for m in warnings
        ), warnings
        assert not any("dev channel" in m for m in warnings), warnings


class TestGetVersionDistOwnership:
    """get_version prefers the distribution that owns the ha_mcp package.

    Regression guard for the live-found ambiguity: both channel dists can
    leave metadata behind (interrupted channel switch / failed best-effort
    uninstall), and the old fixed name order then reported the leftover
    dist's version instead of the installed one.
    """

    def test_prefers_the_dist_whose_files_are_installed(
        self, reported_version: Callable[[], str], installed
    ) -> None:
        installed(
            {
                "ha-mcp": ("1.0.0", None, ["ha_mcp-1.0.0.dist-info/METADATA"]),
                "ha-mcp-dev": ("2.0.0.dev5", None, _WHEEL_FILES),
            }
        )
        assert reported_version() == "2.0.0.dev5"

    def test_declared_top_level_decides_for_an_editable_install(
        self, reported_version: Callable[[], str], installed
    ) -> None:
        """An editable install lists a .pth file, not ha_mcp/, and names the
        package in top_level.txt instead."""
        installed(
            {
                "ha-mcp": ("1.0.0", "tests\n", ["__editable__.ha_mcp.pth"]),
                "ha-mcp-dev": ("2.0.0.dev5", "ha_mcp\n", ["__editable__.pth"]),
            }
        )
        assert reported_version() == "2.0.0.dev5"

    def test_ambiguous_owners_fall_back_to_name_order(
        self, reported_version: Callable[[], str], installed
    ) -> None:
        installed(
            {
                "ha-mcp": ("1.0.0", None, _WHEEL_FILES),
                "ha-mcp-dev": ("2.0.0.dev5", None, _WHEEL_FILES),
            }
        )
        assert reported_version() == "1.0.0"
