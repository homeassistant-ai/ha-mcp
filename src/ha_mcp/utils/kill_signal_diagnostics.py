"""Kill-signal diagnostics for the HA MCP add-on.

Opt-in (gated by the "Advanced debug logging" addon toggle) signal handler
that, on SIGTERM/SIGINT/SIGHUP, captures and logs:

- Signal name + ``si_code`` (USER/KERNEL/QUEUE/TKILL/...).
- Sender PID + its ``comm`` and ``cmdline`` from ``/proc/<pid>``, captured
  via ``sigaction(SA_SIGINFO)`` through ``ctypes`` so we can read
  ``siginfo_t`` (Python's ``signal.signal`` only sees ``signum``).
- ``/proc/self/status`` snapshot of memory + OOM context.

Then chains to whatever handler was previously installed (typically
uvicorn's ``handle_exit`` for SIGTERM/SIGINT) so the server still shuts
down cleanly. SIGHUP, which uvicorn doesn't capture, falls back to
libc-direct ``SIG_DFL`` + re-raise. Linux-only by design.

Without this, the addon only sees that ``mcp.run()`` returned cleanly —
it can't tell whether Supervisor sent SIGTERM, the OOM killer fired, a
container watchdog acted, or something else.

Install ordering
----------------
``signal.signal(...)`` from CPython calls libc's ``sigaction`` with no
``SA_SIGINFO`` flag, which overwrites any ``SA_SIGINFO`` handler we
installed first. uvicorn's ``Server.capture_signals()`` does exactly
this for SIGTERM and SIGINT immediately after ``serve()`` enters. So
installing from ``start.py`` *before* ``mcp.run()`` would silently lose
the SA_SIGINFO bit before any signal arrives.

``schedule_install_after_uvicorn`` spawns a daemon thread that polls
``signal.getsignal`` until uvicorn's handler is detected (or a timeout
elapses), then calls ``install_kill_signal_diagnostics`` which captures
the existing handler and overlays SA_SIGINFO on top. The handler chains
to the captured handler so uvicorn still receives the shutdown signal.

Async-signal-safety
-------------------
The handler is best-effort, not strict POSIX AS-safe:

- It does **not** call any code that takes Python-level locks; the
  ``usage_logger`` ring buffer is intentionally excluded because its
  ``threading.Lock`` is held by the main thread during normal tool calls
  and would deadlock the handler.
- It uses ``os.write(STDERR_FILENO, ...)`` (AS-safe) instead of ``print``.
- It chains to the captured uvicorn handler in pure Python (uvicorn's
  ``handle_exit`` only sets attributes — synchronous, no locks). The
  fallback re-raise path uses ``libc.signal(sig, SIG_DFL)`` and
  ``kill(2)`` directly (both AS-safe).
- ``/proc`` reads use ``open(2)``, which POSIX classifies as not strictly
  AS-safe. In practice the kernel side of ``/proc`` doesn't take
  userspace-allocator locks, so this is acceptable for an opt-in
  diagnostic.

ctypes adds one more theoretical risk: the trampoline acquires the GIL
on entry to Python code. In a single-threaded asyncio event loop (this
addon's shape) the GIL acquisition is a no-op when the handler runs on
the main thread. Multi-threaded callers should evaluate before enabling.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# SIGKILL/SIGSTOP omitted — uncatchable by design.
_INSTRUMENTED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# si_code constants from Linux's <asm-generic/siginfo.h>. Pinned in
# tests so a wrong value can't silently mislabel diagnostics.
_SI_CODE_NAMES = {
    0: "SI_USER",
    0x80: "SI_KERNEL",
    -1: "SI_QUEUE",
    -2: "SI_TIMER",
    -3: "SI_MESGQ",
    -4: "SI_ASYNCIO",
    -5: "SI_SIGIO",
    -6: "SI_TKILL",
}


class _Siginfo(ctypes.Structure):
    """Minimal ``siginfo_t`` for kill-style signals.

    Linux's ``siginfo_t`` is arch-dependent: on architectures without
    ``__ARCH_HAS_SWAPPED_SIGINFO`` (x86, x86_64, arm, aarch64 — all of
    the addon's target arches), the leading layout is ``si_signo``,
    ``si_errno``, ``si_code``, then the ``_kill`` union starting with
    ``si_pid`` / ``si_uid``. Trailing bytes are reserved padding from
    the kernel's ``SI_MAX_SIZE = 128``.
    """

    _fields_ = [
        ("si_signo", ctypes.c_int),
        ("si_errno", ctypes.c_int),
        ("si_code", ctypes.c_int),
        ("_pad0", ctypes.c_int),  # 64-bit alignment for the _kill union
        ("si_pid", ctypes.c_int),
        ("si_uid", ctypes.c_uint),
        # Pad out to the kernel's SI_MAX_SIZE so libc writes the full
        # union without truncating.
        ("_tail", ctypes.c_byte * 104),
    ]


# Pinned by SI_MAX_SIZE in the kernel. If a future ctypes change shifts
# field offsets, fail loudly at import rather than during signal delivery.
assert ctypes.sizeof(_Siginfo) == 128, (
    f"_Siginfo size {ctypes.sizeof(_Siginfo)} != kernel SI_MAX_SIZE 128"
)
assert _Siginfo.si_pid.offset == 16, (
    f"_Siginfo.si_pid offset {_Siginfo.si_pid.offset} != expected 16"
)
assert _Siginfo.si_uid.offset == 20, (
    f"_Siginfo.si_uid offset {_Siginfo.si_uid.offset} != expected 20"
)


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

# SIG_DFL = 0 cast to a function pointer; libc.signal accepts this to
# restore the kernel's default disposition.
_SIG_DFL_PTR = ctypes.c_void_p(0)


def read_proc_status_summary() -> dict[str, str]:
    """Return a small dict of memory/OOM-relevant fields from /proc/self/status.

    Empty dict on non-Linux or unreadable status — callers don't need
    to special-case missing data.
    """
    fields = {"VmRSS", "VmHWM", "VmPeak", "Threads", "State", "oom_score", "oom_score_adj"}
    out: dict[str, str] = {}
    try:
        with open("/proc/self/status", "rb") as f:
            for raw_line in f:
                line = raw_line.decode("utf-8", errors="replace")
                key, _, value = line.partition(":")
                if key in fields:
                    out[key] = value.strip()
    except OSError:
        return {}
    return out


def read_proc_comm(pid: int) -> str:
    """Return the ``comm`` (process name, ≤15 chars) for the given PID.

    Empty string if the PID is gone or /proc isn't available. Reads as
    bytes + ``errors="replace"`` because comm can contain arbitrary
    bytes (set via ``prctl(PR_SET_NAME)``) — strict UTF-8 decode would
    raise on those.
    """
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def read_proc_cmdline(pid: int) -> str:
    """Return the cmdline (argv joined by spaces) for the given PID.

    Cmdline can be more informative than ``comm`` (which is truncated to
    15 chars and often shows just "supervisor" for many distinct
    binaries). Empty string if unavailable.
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
) -> str:
    """Compose the multi-line log block written when a signal is caught."""
    sig_name = signal.Signals(signum).name if signum in signal.Signals.__members__.values() else str(signum)
    code_name = _SI_CODE_NAMES.get(si_code, f"SI_UNKNOWN({si_code})")

    # si_pid == 0 from the kernel means the sender was outside our PID
    # namespace (typically Supervisor or the host) — its PID didn't
    # translate, so /proc/0/{comm,cmdline} can't resolve. Render an
    # explicit label so the diagnostic isn't read as "we failed to
    # capture the sender" — the cross-namespace case is itself the
    # signal in #1109-style reports.
    if sender_pid == 0:
        sender_pid_str = "0 (cross-namespace; likely Supervisor or host process)"
        sender_comm_str = "<cross-namespace>"
        sender_cmdline_str = "<cross-namespace>"
    else:
        sender_pid_str = str(sender_pid)
        sender_comm_str = sender_comm or "<unavailable>"
        sender_cmdline_str = sender_cmdline or "<unavailable>"

    lines = [
        "=" * 80,
        "ADVANCED DEBUG LOGGING — kill-signal diagnostics",
        "=" * 80,
        f"Signal:         {sig_name} ({signum})",
        f"si_code:        {code_name}",
        f"Sender PID:     {sender_pid_str}",
        f"Sender comm:    {sender_comm_str}",
        f"Sender cmdline: {sender_cmdline_str}",
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
    lines.append("=" * 80)
    return "\n".join(lines)


# Module-level reference set so the kernel-installed pointer isn't GC'd
# mid-flight. Comment exists because the variable looks unused — without
# it a future maintainer will delete it and ship a use-after-free.
_handler_refs: list[Any] = []
_libc: Any = None
# Captured at install time so our handler can chain back to whatever
# was installed before (typically uvicorn's handle_exit).
_chained_handlers: dict[int, Any] = {}


def _emit_block_safely(block: str) -> None:
    """Write ``block`` to stderr using only async-signal-safe primitives."""
    payload = (block + "\n").encode("utf-8", errors="replace")
    try:
        os.write(2, payload)
    except OSError:
        pass


def _restore_default_and_reraise(signum: int) -> None:
    """Reset disposition to SIG_DFL via direct libc and re-raise.

    Uses ``libc.signal(signum, SIG_DFL)`` (AS-safe) instead of Python's
    ``signal.signal`` because the latter mutates CPython signal-state
    bookkeeping that assumes main-thread + bytecode-boundary calls.
    """
    if _libc is not None:
        try:
            _libc.signal(int(signum), _SIG_DFL_PTR)
        except OSError:
            pass
    os.kill(os.getpid(), signum)


def _chain_or_reraise(signum: int) -> None:
    """Hand control to the previously-installed handler, or re-raise default.

    Uvicorn's ``handle_exit`` only sets ``self.should_exit`` (synchronous,
    no locks), so calling it directly from this trampoline is safe.
    """
    chained = _chained_handlers.get(signum)
    if callable(chained):
        try:
            chained(signum, None)
            return
        except Exception:
            # Fall through to the default-disposition path so the
            # process still terminates if the chained handler explodes.
            pass
    _restore_default_and_reraise(signum)


def _make_handler() -> Any:
    """Build the C-callable signal handler closure.

    Returns a ``_SignalHandler`` (``ctypes.CFUNCTYPE`` instance), typed
    as ``Any`` because Pyright doesn't accept dynamically-generated
    ctypes function-pointer types in static type expressions.
    """

    def _handler(signum: int, info_ptr: Any, _ucontext: int) -> None:
        try:
            info = info_ptr.contents
            si_code = int(info.si_code)
            sender_pid = int(info.si_pid)
            block = format_diagnostic_block(
                signum=signum,
                si_code=si_code,
                sender_pid=sender_pid,
                sender_comm=read_proc_comm(sender_pid),
                sender_cmdline=read_proc_cmdline(sender_pid),
                proc_status=read_proc_status_summary(),
            )
            _emit_block_safely(block)
        except Exception as exc:  # pragma: no cover — last-resort safety
            try:
                os.write(
                    2,
                    f"advanced_debug_logging handler failed for signal {signum}: {exc!r}\n".encode(
                        "utf-8", errors="replace"
                    ),
                )
            except OSError:
                pass

        _chain_or_reraise(signum)

    return _SignalHandler(_handler)


def install_kill_signal_diagnostics() -> bool:
    """Install the SA_SIGINFO signal handler.

    Captures any previously-installed handler (e.g. uvicorn's
    ``handle_exit``) via ``signal.getsignal`` so the SA_SIGINFO handler
    can chain to it. Idempotent — second call is a no-op.

    Returns True if at least one signal was installed; False on
    non-Linux, missing libc, or if every sigaction call failed. Never
    raises: callers don't need to wrap in try/except. This contract is
    load-bearing — diagnostics must not block addon startup.
    """
    global _libc

    if sys.platform != "linux":
        logger.warning(
            "advanced_debug_logging is Linux-only; skipping signal handler install on %s",
            sys.platform,
        )
        return False

    if _handler_refs:
        logger.warning(
            "advanced_debug_logging: install_kill_signal_diagnostics already called; skipping"
        )
        return True

    try:
        libc_path = ctypes.util.find_library("c")
        if libc_path is None:
            logger.warning("advanced_debug_logging: libc not found; skipping signal handler install")
            return False

        libc = ctypes.CDLL(libc_path, use_errno=True)
        libc.sigaction.restype = ctypes.c_int
        libc.sigaction.argtypes = [ctypes.c_int, ctypes.POINTER(_Sigaction), ctypes.POINTER(_Sigaction)]
        # signal(int, sighandler_t) — used by the handler itself to
        # restore SIG_DFL via the AS-safe libc entry point.
        libc.signal.restype = ctypes.c_void_p
        libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
        _libc = libc

        # Snapshot the existing handler for each instrumented signal
        # before we overwrite. This is what we chain back to so uvicorn
        # (or whoever was there) still receives the shutdown signal.
        for sig in _INSTRUMENTED_SIGNALS:
            existing = signal.getsignal(int(sig))
            if callable(existing):
                _chained_handlers[int(sig)] = existing

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
    except Exception as exc:
        logger.warning(
            "advanced_debug_logging: install failed (%r); continuing without diagnostics",
            exc,
        )
        _handler_refs.clear()
        _chained_handlers.clear()
        _libc = None
        return False

    if installed_for:
        chained_signals = sorted(signal.Signals(s).name for s in _chained_handlers)
        logger.info(
            "advanced_debug_logging enabled — kill-signal diagnostics installed for: %s "
            "(chains to existing handlers for: %s)",
            ", ".join(installed_for),
            ", ".join(chained_signals) or "<none>",
        )
        return True
    _handler_refs.clear()
    _chained_handlers.clear()
    _libc = None
    return False


def schedule_install_after_uvicorn(
    *,
    timeout_secs: float = 10.0,
    poll_interval_secs: float = 0.1,
    install: Callable[[], bool] = install_kill_signal_diagnostics,
) -> threading.Thread:
    """Defer install until uvicorn's ``capture_signals()`` has run.

    uvicorn's ``Server.capture_signals()`` calls
    ``signal.signal(SIGTERM/SIGINT, handle_exit)`` immediately after
    ``Server.serve()`` enters. Python's ``signal.signal`` reaches libc's
    ``sigaction`` *without* ``SA_SIGINFO``, so any handler we installed
    before ``mcp.run()`` would lose its SA_SIGINFO bit before any signal
    arrived. This polls ``signal.getsignal(SIGTERM)`` from a daemon
    thread until uvicorn replaces the default disposition, then calls
    ``install`` so our SA_SIGINFO handler lands on top and chains to
    uvicorn's ``handle_exit``.

    If uvicorn never installs (e.g. addon was started without HTTP
    transport), install runs anyway after ``timeout_secs``.

    Returns the started thread so callers can ``.join()`` in tests.
    """

    def _wait_then_install() -> None:
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            current = signal.getsignal(signal.SIGTERM)
            if callable(current) and current not in (signal.SIG_DFL, signal.SIG_IGN):
                logger.debug(
                    "advanced_debug_logging: detected uvicorn signal handler; installing on top"
                )
                install()
                return
            time.sleep(poll_interval_secs)
        logger.info(
            "advanced_debug_logging: uvicorn handler not detected within %.1fs; installing anyway",
            timeout_secs,
        )
        install()

    thread = threading.Thread(
        target=_wait_then_install,
        name="kill-signal-diagnostics-install",
        daemon=True,
    )
    thread.start()
    return thread
