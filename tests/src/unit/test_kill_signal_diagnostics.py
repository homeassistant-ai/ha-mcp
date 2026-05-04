"""Unit tests for the advanced-debug-logging kill-signal diagnostics module.

The module installs a Linux-only `sigaction(SA_SIGINFO)` handler. We don't
exercise the kernel signal path in CI; instead we verify the helpers that
build the diagnostic block — `/proc` parsing, formatting, and the install
path's platform gating.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from ha_mcp.utils.kill_signal_diagnostics import (
    format_diagnostic_block,
    install_kill_signal_diagnostics,
    read_proc_cmdline,
    read_proc_comm,
    read_proc_status_summary,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


class TestReadProcStatusSummary:
    def test_returns_only_whitelisted_fields(self, tmp_path: Path) -> None:
        sample = _write(
            tmp_path,
            "status",
            (
                "Name:\tha_mcp\n"
                "State:\tS (sleeping)\n"
                "Pid:\t1\n"
                "VmPeak:\t  524288 kB\n"
                "VmRSS:\t  131072 kB\n"
                "VmHWM:\t  262144 kB\n"
                "Threads:\t9\n"
                "oom_score:\t0\n"
                "oom_score_adj:\t-500\n"
                "SigPnd:\t0000000000000000\n"
            ),
        )

        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value = sample.open(encoding="utf-8")
            out = read_proc_status_summary()

        assert out["State"] == "S (sleeping)"
        assert out["VmRSS"] == "131072 kB"
        assert out["VmHWM"] == "262144 kB"
        assert out["VmPeak"] == "524288 kB"
        assert out["Threads"] == "9"
        assert out["oom_score"] == "0"
        assert out["oom_score_adj"] == "-500"
        # Pid and SigPnd were not in the whitelist.
        assert "Pid" not in out
        assert "SigPnd" not in out
        assert "Name" not in out

    def test_returns_empty_dict_when_proc_missing(self) -> None:
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert read_proc_status_summary() == {}


class TestReadProcComm:
    def test_returns_stripped_comm(self) -> None:
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = "supervisor\n"
            assert read_proc_comm(42) == "supervisor"

    def test_returns_empty_for_invalid_pid(self) -> None:
        # Don't touch /proc for sentinel/non-positive PIDs (kernel signal
        # delivery can present si_pid=0 for kernel-originated signals).
        assert read_proc_comm(0) == ""
        assert read_proc_comm(-1) == ""

    def test_returns_empty_when_pid_gone(self) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert read_proc_comm(99999) == ""


class TestReadProcCmdline:
    def test_replaces_nul_separators_with_spaces(self) -> None:
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = (
                b"/usr/bin/python3\x00/app/start.py\x00--foo\x00"
            )
            assert read_proc_cmdline(42) == "/usr/bin/python3 /app/start.py --foo"

    def test_returns_empty_for_invalid_pid(self) -> None:
        assert read_proc_cmdline(0) == ""

    def test_returns_empty_when_unreadable(self) -> None:
        with patch("builtins.open", side_effect=PermissionError):
            assert read_proc_cmdline(1) == ""


class TestFormatDiagnosticBlock:
    def test_includes_signal_name_and_sender_info(self) -> None:
        block = format_diagnostic_block(
            signum=15,  # SIGTERM
            si_code=0,  # SI_USER
            sender_pid=42,
            sender_comm="supervisor",
            sender_cmdline="/usr/bin/supervisor --foo",
            proc_status={"VmRSS": "131072 kB", "Threads": "9", "oom_score_adj": "-500"},
            recent_tool_logs=[],
            startup_logs=[],
        )

        assert "SIGTERM" in block
        assert "SI_USER" in block
        assert "Sender PID:     42" in block
        assert "supervisor" in block
        assert "/usr/bin/supervisor --foo" in block
        assert "VmRSS: 131072 kB" in block
        assert "oom_score_adj: -500" in block

    def test_falls_back_when_sender_metadata_missing(self) -> None:
        block = format_diagnostic_block(
            signum=15,
            si_code=-1,  # SI_KERNEL
            sender_pid=0,
            sender_comm="",
            sender_cmdline="",
            proc_status={},
            recent_tool_logs=[],
            startup_logs=[],
        )

        assert "SI_KERNEL" in block
        assert "Sender comm:    <unavailable>" in block
        assert "Sender cmdline: <unavailable>" in block
        assert "<unavailable — non-Linux or /proc not mounted>" in block
        assert "<ring buffer empty>" in block
        assert "<startup buffer empty>" in block

    def test_unknown_si_code_is_labeled_with_value(self) -> None:
        block = format_diagnostic_block(
            signum=15,
            si_code=99,
            sender_pid=1,
            sender_comm="init",
            sender_cmdline="/sbin/init",
            proc_status={},
            recent_tool_logs=[],
            startup_logs=[],
        )
        # Unknown codes should still surface the raw value so reporters can look it up.
        assert "SI_UNKNOWN(99)" in block

    def test_truncates_startup_logs_to_recent_tail(self) -> None:
        startup = [
            {"elapsed_seconds": i, "level": "INFO", "message": f"msg{i}"} for i in range(40)
        ]
        block = format_diagnostic_block(
            signum=1,
            si_code=0,
            sender_pid=1,
            sender_comm="init",
            sender_cmdline="",
            proc_status={},
            recent_tool_logs=[],
            startup_logs=startup,
        )
        # Only the last 15 entries should appear — earlier ones suppressed
        # to keep the kill block bounded.
        assert "msg0" not in block
        assert "msg25" in block
        assert "msg39" in block


class TestInstallKillSignalDiagnostics:
    def test_returns_false_on_non_linux(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert install_kill_signal_diagnostics() is False

    def test_returns_false_when_libc_lookup_fails(self) -> None:
        # On Linux, exercise the libc-not-found branch by stubbing
        # ctypes.util.find_library to None.
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        with patch("ha_mcp.utils.kill_signal_diagnostics.ctypes.util.find_library", return_value=None):
            assert install_kill_signal_diagnostics() is False
