"""Tests for ha_mcp.utils.mcp_client_host — host-app detection over stdio."""

from __future__ import annotations

import plistlib
import sys
from types import SimpleNamespace

import psutil
import pytest

from ha_mcp.utils import mcp_client_host
from ha_mcp.utils.mcp_client_host import classify_host, detect_client_host


class TestClassifyHost:
    def test_macos_claude_bundle_reads_version_from_info_plist(self, tmp_path):
        bundle = tmp_path / "Claude.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        with (bundle / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "2.110.0"}, handle)
        exe = str(bundle / "Contents" / "MacOS" / "Claude")

        assert classify_host(exe, "Claude") == {
            "name": "Claude Desktop",
            "version": "2.110.0",
            "evidence": exe,
        }

    def test_macos_disclaimer_helper_is_the_bundle_not_a_wrapper(self, tmp_path):
        # Desktop launches stdio servers through Contents/Helpers/disclaimer;
        # the bundle path wins over the wrapper skip-list so the version is
        # still read off the app.
        bundle = tmp_path / "Claude.app"
        (bundle / "Contents" / "Helpers").mkdir(parents=True)
        with (bundle / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "1.52386.6"}, handle)
        exe = str(bundle / "Contents" / "Helpers" / "disclaimer")

        host = classify_host(exe, "disclaimer")
        assert host is not None
        assert host["name"] == "Claude Desktop"
        assert host["version"] == "1.52386.6"

    def test_macos_bundle_without_readable_plist_reports_unknown(self, tmp_path):
        exe = str(tmp_path / "Claude.app" / "Contents" / "MacOS" / "Claude")
        assert classify_host(exe, "Claude") == {
            "name": "Claude Desktop",
            "version": "unknown",
            "evidence": exe,
        }

    @pytest.mark.parametrize(
        ("exe", "version"),
        [
            (
                r"C:\Users\u\AppData\Local\AnthropicClaude\app-2.110.0\claude.exe",
                "2.110.0",
            ),
            (
                r"C:\Program Files\WindowsApps\Claude_1.52386.6_x64__pzs8sxrjxfjjc\app\Claude.exe",
                "1.52386.6",
            ),
        ],
    )
    def test_windows_install_layouts_carry_the_version(self, exe, version):
        assert classify_host(exe, "claude.exe") == {
            "name": "Claude Desktop",
            "version": version,
            "evidence": exe,
        }

    def test_windows_claude_code_binary_is_not_mislabelled_as_desktop(self):
        # Claude Code's native Windows install is also claude.exe; without the
        # Desktop install layout in the path only the bare name is reported.
        exe = r"C:\Users\u\.local\bin\claude.exe"
        assert classify_host(exe, "claude.exe") == {
            "name": "claude.exe",
            "version": "unknown",
            "evidence": exe,
        }

    def test_squirrel_layout_of_another_app_is_not_claude_desktop(self):
        exe = r"C:\Users\u\AppData\Local\Discord\app-1.0.9\Discord.exe"
        assert classify_host(exe, "Discord.exe") == {
            "name": "Discord.exe",
            "version": "unknown",
            "evidence": exe,
        }

    @pytest.mark.parametrize(
        "name",
        [
            "sh",
            "bash",
            "zsh",
            "uv",
            "uvx",
            "uvx.exe",
            "python3.13",
            "python.exe",
            "node",
            "npx.cmd",
            "cmd.exe",
            "powershell.exe",
            "mcp-proxy",
            "ha-mcp",
        ],
    )
    def test_launch_chain_wrappers_are_skipped(self, name):
        assert classify_host(f"/usr/bin/{name}", name) is None

    def test_unknown_host_is_reported_by_name_with_unknown_version(self):
        assert classify_host(
            "/Applications/Cursor.app/Contents/MacOS/Cursor", "Cursor"
        ) == {
            "name": "Cursor",
            "version": "unknown",
            "evidence": "/Applications/Cursor.app/Contents/MacOS/Cursor",
        }

    def test_name_only_when_exe_is_unreadable(self):
        # MSIX binaries raise AccessDenied on exe(); the bare name still
        # identifies the host and becomes the evidence.
        assert classify_host("", "Claude.exe") == {
            "name": "Claude.exe",
            "version": "unknown",
            "evidence": "Claude.exe",
        }


class _FakeProc:
    def __init__(self, exe: str, name: str, exe_error: Exception | None = None):
        self._exe = exe
        self._name = name
        self._exe_error = exe_error

    def exe(self) -> str:
        if self._exe_error is not None:
            raise self._exe_error
        return self._exe

    def name(self) -> str:
        return self._name


class TestDetectClientHost:
    @pytest.fixture
    def ancestors(self, monkeypatch):
        chain: list[_FakeProc] = []
        monkeypatch.setattr(
            psutil, "Process", lambda: SimpleNamespace(parents=lambda: chain)
        )
        return chain

    def test_walks_past_wrappers_to_the_first_host(self, ancestors, tmp_path):
        bundle = tmp_path / "Claude.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        with (bundle / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "2.110.0"}, handle)
        ancestors.extend(
            [
                _FakeProc("/opt/homebrew/bin/uvx", "uvx"),
                _FakeProc("/bin/sh", "sh"),
                _FakeProc(str(bundle / "Contents" / "MacOS" / "Claude"), "Claude"),
                _FakeProc("/sbin/launchd", "launchd"),
            ]
        )

        assert detect_client_host()["version"] == "2.110.0"

    def test_access_denied_exe_falls_back_to_name(self, ancestors):
        ancestors.extend(
            [
                _FakeProc("", "uv.exe"),
                _FakeProc("", "Claude.exe", exe_error=psutil.AccessDenied(pid=1)),
            ]
        )

        assert detect_client_host() == {
            "name": "Claude.exe",
            "version": "unknown",
            "evidence": "Claude.exe",
        }

    def test_only_wrappers_in_the_chain_returns_empty(self, ancestors):
        ancestors.extend([_FakeProc("/usr/bin/uv", "uv"), _FakeProc("/bin/sh", "sh")])
        assert detect_client_host() == {}

    def test_unreadable_process_tree_returns_empty(self, monkeypatch):
        def boom():
            raise psutil.NoSuchProcess(pid=0)

        monkeypatch.setattr(psutil, "Process", boom)
        assert detect_client_host() == {}

    def test_missing_psutil_returns_empty(self, monkeypatch):
        # ``sys.modules[name] = None`` makes ``import psutil`` raise
        # ImportError, which is how a runtime without the wheel looks.
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert mcp_client_host.detect_client_host() == {}
