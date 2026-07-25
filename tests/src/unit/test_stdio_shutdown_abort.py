"""Regression tests for the stdio shutdown abort (issue #2027).

The MCP SDK's stdio transport reads stdin through anyio's worker-thread
pool, so a daemon thread parked in a blocking stdin read holds the
``BufferedReader`` lock whenever no message is in flight. If the process
reaches interpreter finalization in that state, CPython aborts with
``Fatal Python error: _enter_buffered_busy`` — SIGABRT, which macOS turns
into a user-visible crash dialog. ``ha_mcp.__main__._force_exit`` skips
finalization to neutralize this; a shutdown watchdog bounds the related
hang where the SDK's stdio teardown never completes at all.

Everything here spawns real subprocesses: the abort happens inside
interpreter teardown, which cannot be reproduced in-process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from io import BufferedReader
from pathlib import Path

import pytest

_FATAL = "Fatal Python error"

# Parks a daemon thread in a blocking stdin read — the exact state the MCP
# SDK's stdio transport is in between client messages. The Event narrows
# the start-up race; the sleep afterwards lets the thread enter read() and
# take the BufferedReader lock before the main thread exits.
_PARKED_READER_PREAMBLE = """\
import sys, threading, time
ready = threading.Event()
def _park():
    ready.set()
    sys.stdin.buffer.read()
threading.Thread(target=_park, daemon=True).start()
ready.wait(5)
time.sleep(0.5)
"""


def _run_with_open_stdin(script: str, timeout: float = 60) -> tuple[int, str]:
    """Run ``script`` in a subprocess whose stdin stays open until it exits.

    Returns (returncode, stderr). Crucially does NOT use ``communicate()``
    while the child is alive — that closes stdin, which delivers EOF, wakes
    the parked reader thread, and destroys the scenario under test.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
    _, stderr = proc.communicate(timeout=10)
    return proc.returncode, stderr.decode(errors="replace")


@pytest.mark.timeout(120)
class TestForceExitMechanism:
    """The finalization abort is real, and _force_exit sidesteps it."""

    def test_plain_exit_with_parked_reader_aborts(self) -> None:
        """Control: sys.exit() in the parked-reader state aborts the process.

        This is the bug being guarded against (no anyio involved — the abort
        is CPython finalization behavior). If a future CPython release
        changes buffered-IO finalization so this passes cleanly (returncode
        0, no fatal error), the _force_exit workaround in ha_mcp.__main__
        can likely be removed. It can also fail if the reader thread loses
        the startup race and is not yet inside read() at exit — treat that
        as a race to investigate, not as the upstream fix having landed.
        """
        code, stderr = _run_with_open_stdin(_PARKED_READER_PREAMBLE + "sys.exit(0)")
        assert code != 0
        assert "_enter_buffered_busy" in stderr

    def test_force_exit_with_parked_reader_is_clean(self) -> None:
        script = (
            _PARKED_READER_PREAMBLE
            + "from ha_mcp.__main__ import _force_exit\n_force_exit(0)"
        )
        code, stderr = _run_with_open_stdin(script)
        assert code == 0, f"stderr:\n{stderr}"
        assert _FATAL not in stderr

    def test_force_exit_propagates_exit_code(self) -> None:
        script = (
            _PARKED_READER_PREAMBLE
            + "from ha_mcp.__main__ import _force_exit\n_force_exit(7)"
        )
        code, stderr = _run_with_open_stdin(script)
        assert code == 7, f"stderr:\n{stderr}"
        assert _FATAL not in stderr


def _drain(stream: BufferedReader, sink: bytearray) -> None:
    """Continuously read a pipe so the child never blocks on a full buffer."""
    while chunk := stream.read(4096):
        sink.extend(chunk)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics required (SIGTERM); CI runs this on Linux",
)
@pytest.mark.timeout(180)
def test_stdio_server_sigterm_exits_cleanly(tmp_path: Path) -> None:
    """Full-server regression test: SIGTERM mid-session must exit cleanly.

    Reproduces the issue #2027 session-end sequence: the server is up, has
    answered ``initialize``, and its stdin reader is parked awaiting the next
    message; the client then goes away (SIGTERM). Without the fixes this
    fails two ways: ``sys.exit`` reaching finalization aborts (returncode
    -6, the macOS crash-dialog variant), or the SDK's stdio teardown never
    completes while the stdin read is in flight and the process never exits
    (the leftover-instances variant). The shutdown watchdog bounds the
    latter, so the process must exit 0 well inside the wait below.
    """
    env = os.environ.copy()
    env.update(
        {
            # Unreachable on purpose: startup must not depend on a live HA.
            "HOMEASSISTANT_URL": "http://127.0.0.1:1",
            "HOMEASSISTANT_TOKEN": "test-token",
            "HA_MCP_CONFIG_DIR": str(tmp_path),
            # No detached sidecar process and no PyPI call from CI.
            "HA_MCP_DISABLE_SETTINGS_UI": "1",
            "HA_MCP_DISABLE_UPDATE_CHECK": "1",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "ha_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    assert proc.stderr is not None
    stdout = proc.stdout
    stderr_sink = bytearray()
    threading.Thread(
        target=_drain, args=(proc.stderr, stderr_sink), daemon=True
    ).start()

    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest-2027", "version": "0"},
            },
        }
        proc.stdin.write(json.dumps(initialize).encode() + b"\n")
        proc.stdin.flush()

        # First stdout line is the initialize response (stdout is
        # protocol-only in stdio transport; logs go to stderr). Reading it
        # proves the server is fully up with the stdin reader active.
        response: list[bytes] = []
        reader = threading.Thread(
            target=lambda: response.append(stdout.readline()), daemon=True
        )
        reader.start()
        reader.join(timeout=90)
        assert response and response[0], "no initialize response before timeout"
        assert b'"serverInfo"' in response[0]

        # Let the transport dispatch the next readline so the reader thread
        # is parked holding the stdin lock — the crash-triggering state.
        time.sleep(1.0)

        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    stderr_text = stderr_sink.decode(errors="replace")
    assert returncode == 0, (
        f"expected clean exit after SIGTERM, got {returncode}\nstderr:\n{stderr_text}"
    )
    assert _FATAL not in stderr_text
    assert "_enter_buffered_busy" not in stderr_text
