"""Unit tests for the advanced-debug-logging kill-signal diagnostics module.

The module installs a Linux-only sigaction(SA_SIGINFO) handler. We don't
exercise the kernel signal path; instead we verify the helpers that
build the diagnostic block (/proc parsing, formatting, the install path's
gating, and the kernel ABI constants the diagnostic block depends on).
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ha_mcp.utils import kill_signal_diagnostics as ksd
from ha_mcp.utils.kill_signal_diagnostics import (
    _SI_CODE_NAMES,
    _Siginfo,
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


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Each test starts with a clean install state (idempotency tests need this)."""
    ksd._handler_refs.clear()
    ksd._libc = None
    yield
    ksd._handler_refs.clear()
    ksd._libc = None


class TestSiCodeAbiPinning:
    """Pin _SI_CODE_NAMES values to <asm-generic/siginfo.h>.

    A wrong value here silently mislabels the diagnostic block — defeating
    the feature. Pinning each value catches "fix this and break it again"
    regressions.
    """

    @pytest.mark.parametrize(
        ("code", "name"),
        [
            (0, "SI_USER"),
            (0x80, "SI_KERNEL"),
            (-1, "SI_QUEUE"),
            (-2, "SI_TIMER"),
            (-3, "SI_MESGQ"),
            (-4, "SI_ASYNCIO"),
            (-5, "SI_SIGIO"),
            (-6, "SI_TKILL"),
        ],
    )
    def test_kernel_abi_constants(self, code: int, name: str) -> None:
        assert _SI_CODE_NAMES[code] == name


class TestSiginfoLayout:
    """Pin the load-bearing offsets and size at the test level too.

    The module has matching import-time asserts; this is the regression
    guard if those ever get loosened.
    """

    def test_size_matches_kernel_si_max_size(self) -> None:
        assert ctypes.sizeof(_Siginfo) == 128

    def test_si_pid_offset(self) -> None:
        assert _Siginfo.si_pid.offset == 16

    def test_si_uid_offset(self) -> None:
        assert _Siginfo.si_uid.offset == 20


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
            mock_open.return_value = sample.open("rb")
            out = read_proc_status_summary()

        assert out["State"] == "S (sleeping)"
        assert out["VmRSS"] == "131072 kB"
        assert out["VmHWM"] == "262144 kB"
        assert out["VmPeak"] == "524288 kB"
        assert out["Threads"] == "9"
        assert out["oom_score"] == "0"
        assert out["oom_score_adj"] == "-500"
        assert "Pid" not in out
        assert "SigPnd" not in out
        assert "Name" not in out

    def test_returns_empty_dict_when_proc_missing(self) -> None:
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert read_proc_status_summary() == {}


class TestReadProcComm:
    def test_returns_stripped_comm(self) -> None:
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b"supervisor\n"
            assert read_proc_comm(42) == "supervisor"

    def test_handles_non_utf8_process_name(self) -> None:
        # PR_SET_NAME accepts arbitrary bytes; strict-UTF-8 decode would
        # raise on those. Verify we fall back to replacement chars.
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b"\xff\xfe\xfd\n"
            result = read_proc_comm(42)
            # Don't assert the exact replacement char output; just that
            # we returned a string and didn't raise.
            assert isinstance(result, str)

    def test_returns_empty_for_invalid_pid(self) -> None:
        # Don't touch /proc for sentinel/non-positive PIDs (kernel
        # signal delivery presents si_pid=0 for kernel-originated
        # signals).
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
            si_code=0x80,  # SI_KERNEL
            sender_pid=0,
            sender_comm="",
            sender_cmdline="",
            proc_status={},
        )

        assert "SI_KERNEL" in block
        assert "Sender comm:    <unavailable>" in block
        assert "Sender cmdline: <unavailable>" in block
        assert "<unavailable — non-Linux or /proc not mounted>" in block

    @pytest.mark.parametrize(
        ("code", "name"),
        [
            (0, "SI_USER"),
            (0x80, "SI_KERNEL"),
            (-1, "SI_QUEUE"),
            (-6, "SI_TKILL"),
        ],
    )
    def test_known_si_codes_render_symbolic_names(self, code: int, name: str) -> None:
        block = format_diagnostic_block(
            signum=15,
            si_code=code,
            sender_pid=1,
            sender_comm="x",
            sender_cmdline="",
            proc_status={},
        )
        assert name in block

    def test_unknown_si_code_is_labeled_with_value(self) -> None:
        block = format_diagnostic_block(
            signum=15,
            si_code=99,
            sender_pid=1,
            sender_comm="init",
            sender_cmdline="/sbin/init",
            proc_status={},
        )
        # Unknown codes should still surface the raw value so reporters
        # can look it up in <asm-generic/siginfo.h>.
        assert "SI_UNKNOWN(99)" in block


class TestInstallKillSignalDiagnostics:
    def test_returns_false_on_non_linux(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert install_kill_signal_diagnostics() is False

    def test_returns_false_when_libc_lookup_fails(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        with patch("ha_mcp.utils.kill_signal_diagnostics.ctypes.util.find_library", return_value=None):
            assert install_kill_signal_diagnostics() is False

    def test_install_never_raises_on_libc_load_error(self) -> None:
        # Diagnostics must not block addon startup. If libc loading or
        # symbol resolution fails, the install function returns False
        # rather than propagating.
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        with patch(
            "ha_mcp.utils.kill_signal_diagnostics.ctypes.CDLL",
            side_effect=OSError("simulated libc load failure"),
        ):
            assert install_kill_signal_diagnostics() is False

    def test_happy_path_installs_for_all_signals(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        fake_libc = MagicMock()
        fake_libc.sigaction.return_value = 0  # success
        with patch(
            "ha_mcp.utils.kill_signal_diagnostics.ctypes.util.find_library", return_value="libc.so.6"
        ), patch("ha_mcp.utils.kill_signal_diagnostics.ctypes.CDLL", return_value=fake_libc):
            assert install_kill_signal_diagnostics() is True

        # Three signals (SIGTERM, SIGINT, SIGHUP) → three sigaction calls.
        assert fake_libc.sigaction.call_count == 3
        # Two refs pinned: handler + sigaction struct.
        assert len(ksd._handler_refs) == 2

    def test_partial_install_returns_true_when_any_signal_succeeds(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        fake_libc = MagicMock()
        # First two calls succeed, third fails.
        fake_libc.sigaction.side_effect = [0, 0, 1]
        with patch(
            "ha_mcp.utils.kill_signal_diagnostics.ctypes.util.find_library", return_value="libc.so.6"
        ), patch("ha_mcp.utils.kill_signal_diagnostics.ctypes.CDLL", return_value=fake_libc), patch(
            "ha_mcp.utils.kill_signal_diagnostics.ctypes.get_errno", return_value=22
        ):
            assert install_kill_signal_diagnostics() is True

    def test_idempotent_second_call_is_noop(self) -> None:
        if sys.platform != "linux":
            pytest.skip("Linux-only branch")
        fake_libc = MagicMock()
        fake_libc.sigaction.return_value = 0
        with patch(
            "ha_mcp.utils.kill_signal_diagnostics.ctypes.util.find_library", return_value="libc.so.6"
        ), patch("ha_mcp.utils.kill_signal_diagnostics.ctypes.CDLL", return_value=fake_libc):
            assert install_kill_signal_diagnostics() is True
            first_call_count = fake_libc.sigaction.call_count
            # Second call short-circuits before touching libc.
            assert install_kill_signal_diagnostics() is True
            assert fake_libc.sigaction.call_count == first_call_count
