"""Kill-signal diagnostics for the HA MCP add-on.

Opt-in (gated by the `Advanced debug logging` add-on toggle) signal handler
that, on SIGTERM/SIGINT/SIGHUP, captures and logs:

- Signal name, `si_code`, and the sender PID/comm/cmdline (via Linux
  `sigaction` + `SA_SIGINFO` through `ctypes` — Python's `signal.signal()`
  doesn't expose `siginfo_t`).
- `/proc/self/status` snapshot of memory and OOM context.
- Recent tool-usage and startup log entries (in-memory, no extra collection).

Then re-raises Python's default disposition for the signal so Uvicorn still
shuts down cleanly. Linux-only by design (HA add-ons run on HAOS).

The handler exists to surface *who* terminated the process when "watchdog
disabled, server stops anyway" reports come in (see issue #1109). Without
this, the add-on can only see that mcp.run() returned cleanly — not who
asked for it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import signal
import sys
from typing import Any

from .usage_logger import get_recent_logs, get_startup_logs

logger = logging.getLogger(__name__)

# Signals we want to instrument. SIGKILL/SIGSTOP are uncatchable by design.
_INSTRUMENTED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# si_code constants (from <bits/siginfo-consts.h>). The values are stable across
# glibc/musl and the Linux kernel ABI; replicating here avoids a libc lookup.
_SI_CODE_NAMES = {
    0: "SI_USER",  # kill(2), raise(3)
    -1: "SI_KERNEL",
    -2: "SI_QUEUE",  # sigqueue(3)
    -3: "SI_TIMER",
    -4: "SI_MESGQ",
    -5: "SI_ASYNCIO",
    -6: "SI_SIGIO",
    -7: "SI_TKILL",  # tkill(2), tgkill(2)
}


class _Siginfo(ctypes.Structure):
    """Minimal `siginfo_t` layout — we only need the leading kill-related fields.

    The real struct is a tagged union with many cases; the first four ints
    plus si_pid/si_uid are always present in the layout for kill-style signals
    on Linux glibc/musl. We allocate a generous tail so writes past the union
    boundary by the kernel can't corrupt adjacent stack/heap.
    """

    _fields_ = [
        ("si_signo", ctypes.c_int),
        ("si_errno", ctypes.c_int),
        ("si_code", ctypes.c_int),
        ("_pad0", ctypes.c_int),  # alignment to 8 on 64-bit
        ("si_pid", ctypes.c_int),
        ("si_uid", ctypes.c_uint),
        ("_tail", ctypes.c_byte * 116),  # rest of siginfo_t (sigaction reserves 128 bytes)
    ]


_SignalHandler = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(_Siginfo), ctypes.c_void_p)


class _Sigaction(ctypes.Structure):
    _fields_ = [
        ("sa_sigaction", _SignalHandler),
        ("sa_mask", ctypes.c_byte * 128),  # sigset_t — opaque, zeroed
        ("sa_flags", ctypes.c_int),
        ("sa_restorer", ctypes.c_void_p),
    ]


_SA_SIGINFO = 0x00000004
_SA_RESTART = 0x10000000


def read_proc_status_summary() -> dict[str, str]:
    """Return a small dict of memory/OOM-relevant fields from /proc/self/status.

    Returns an empty dict on non-Linux or if /proc/self/status is unreadable
    so callers don't need to special-case missing data.
    """
    fields = {"VmRSS", "VmHWM", "VmPeak", "Threads", "State", "oom_score", "oom_score_adj"}
    out: dict[str, str] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in fields:
                    out[key] = value.strip()
    except OSError:
        return {}
    return out


def read_proc_comm(pid: int) -> str:
    """Return the `comm` (process name, max 15 chars) for the given PID.

    Returns an empty string if the PID is gone or /proc isn't available.
    """
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_proc_cmdline(pid: int) -> str:
    """Return the cmdline (argv joined by spaces) for the given PID.

    Cmdline can be more informative than comm (which is truncated to 15 chars
    and often shows just "supervisor" for many distinct binaries). Returns an
    empty string if unavailable.
    """
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def format_diagnostic_block(
    *,
    signum: int,
    si_code: int,
    sender_pid: int,
    sender_comm: str,
    sender_cmdline: str,
    proc_status: dict[str, str],
    recent_tool_logs: list[dict[str, Any]],
    startup_logs: list[dict[str, Any]],
) -> str:
    """Compose the multi-line log block written when a signal is caught."""
    sig_name = signal.Signals(signum).name if signum in signal.Signals.__members__.values() else str(signum)
    code_name = _SI_CODE_NAMES.get(si_code, f"SI_UNKNOWN({si_code})")

    lines = [
        "=" * 80,
        "ADVANCED DEBUG LOGGING — kill-signal diagnostics",
        "=" * 80,
        f"Signal:         {sig_name} ({signum})",
        f"si_code:        {code_name}",
        f"Sender PID:     {sender_pid}",
        f"Sender comm:    {sender_comm or '<unavailable>'}",
        f"Sender cmdline: {sender_cmdline or '<unavailable>'}",
        "",
        "Process state (from /proc/self/status):",
    ]
    if proc_status:
        lines.extend(
            f"  {key}: {proc_status[key]}"
            for key in ("State", "VmRSS", "VmHWM", "VmPeak", "Threads", "oom_score", "oom_score_adj")
            if key in proc_status
        )
    else:
        lines.append("  <unavailable — non-Linux or /proc not mounted>")

    lines.append("")
    lines.append(f"Last {len(recent_tool_logs)} tool calls:")
    if recent_tool_logs:
        for entry in recent_tool_logs:
            ts = entry.get("timestamp", "?")
            tool = entry.get("tool_name", "?")
            ok = "OK" if entry.get("success") else "FAIL"
            ms = entry.get("execution_time_ms", 0)
            lines.append(f"  {ts} | {tool} | {ok} | {ms:.1f}ms")
    else:
        lines.append("  <ring buffer empty>")

    lines.append("")
    lines.append(f"Recent startup log ({len(startup_logs)} entries):")
    if startup_logs:
        for entry in startup_logs[-15:]:  # tail to keep the block bounded
            ts = entry.get("elapsed_seconds", "?")
            level = entry.get("level", "?")
            msg = entry.get("message", "")
            lines.append(f"  +{ts}s | {level} | {msg}")
    else:
        lines.append("  <startup buffer empty>")

    lines.append("=" * 80)
    return "\n".join(lines)


# Keep references to ctypes objects for the lifetime of the process so the
# kernel-installed pointer isn't garbage-collected mid-flight.
_handler_refs: list[Any] = []


def _make_handler() -> Any:
    """Build the C-callable signal handler closure.

    Returns a ``_SignalHandler`` (a ``ctypes.CFUNCTYPE`` instance). Annotated
    as ``Any`` because Pyright doesn't accept dynamically-generated ctypes
    function pointer types in static type expressions.
    """

    def _handler(signum: int, info_ptr: Any, _ucontext: int) -> None:
        # Keep the handler small and async-signal-safe-ish: collect data, log,
        # then re-raise the signal with the default disposition so Uvicorn
        # observes a normal shutdown.
        try:
            info = info_ptr.contents
            si_code = int(info.si_code)
            sender_pid = int(info.si_pid)
            sender_comm = read_proc_comm(sender_pid)
            sender_cmdline = read_proc_cmdline(sender_pid)
            proc_status = read_proc_status_summary()
            try:
                recent = get_recent_logs(max_entries=20)
            except Exception:
                recent = []
            try:
                startup = get_startup_logs()
            except Exception:
                startup = []

            block = format_diagnostic_block(
                signum=signum,
                si_code=si_code,
                sender_pid=sender_pid,
                sender_comm=sender_comm,
                sender_cmdline=sender_cmdline,
                proc_status=proc_status,
                recent_tool_logs=recent,
                startup_logs=startup,
            )
            # Use stderr directly: logging may have async handlers that don't
            # flush before the process re-raises and exits.
            print(block, file=sys.stderr, flush=True)
        except Exception as exc:  # pragma: no cover — last-resort safety
            print(
                f"advanced_debug_logging handler failed: {exc!r}",
                file=sys.stderr,
                flush=True,
            )

        # Restore default disposition and re-raise so the process exits the
        # way Uvicorn (and Supervisor) expect.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return _SignalHandler(_handler)


def install_kill_signal_diagnostics() -> bool:
    """Install the SA_SIGINFO signal handler. Returns True if installed.

    Logs a warning and returns False on non-Linux platforms or if libc
    lookup fails — callers don't need to special-case those environments.
    """
    if sys.platform != "linux":
        logger.warning(
            "advanced_debug_logging is Linux-only; skipping signal handler install on %s",
            sys.platform,
        )
        return False

    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        logger.warning("advanced_debug_logging: libc not found; skipping signal handler install")
        return False

    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.sigaction.restype = ctypes.c_int
    libc.sigaction.argtypes = [ctypes.c_int, ctypes.POINTER(_Sigaction), ctypes.POINTER(_Sigaction)]

    handler = _make_handler()
    _handler_refs.append(handler)

    sa = _Sigaction()
    ctypes.memset(ctypes.byref(sa), 0, ctypes.sizeof(sa))
    sa.sa_sigaction = handler
    sa.sa_flags = _SA_SIGINFO | _SA_RESTART
    _handler_refs.append(sa)

    installed_for: list[str] = []
    for sig in _INSTRUMENTED_SIGNALS:
        rc = libc.sigaction(int(sig), ctypes.byref(sa), None)
        if rc != 0:
            err = ctypes.get_errno()
            logger.warning(
                "advanced_debug_logging: sigaction(%s) failed: errno=%d",
                sig.name,
                err,
            )
            continue
        installed_for.append(sig.name)

    if installed_for:
        logger.info(
            "advanced_debug_logging enabled — kill-signal diagnostics installed for: %s",
            ", ".join(installed_for),
        )
        return True
    return False
