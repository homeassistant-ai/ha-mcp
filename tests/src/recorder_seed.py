"""A recorder database for an E2E session, written by the pinned Home Assistant Core."""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
# In the unit tree, so the real-Core rules that tree is checked against cover it.
_GENERATOR = "src/unit/recorder_seed_generator.py"
_PREFIX = "ha-mcp-recorder-seed-"
_TIMEOUT = 600
_STALE_AFTER = 24 * 3600
_TAIL_LINES = 100
_own_directory: list[Path] = []


def _session_directory() -> Path:
    """The xdist run's shared directory, or one this process created for itself."""
    if run := os.environ.get("PYTEST_XDIST_TESTRUNUID"):
        directory = Path(tempfile.gettempdir()) / f"{_PREFIX}{run}"
        directory.mkdir(exist_ok=True)
        return directory
    if not _own_directory:
        _own_directory.append(Path(tempfile.mkdtemp(prefix=_PREFIX)))
    return _own_directory[0]


def session_recorder_seed() -> Path:
    """Return this session's recorder database: one sqlite file, no sidecars.

    Generated once per pytest session and shared by its xdist workers. Raises
    RuntimeError when generation fails, in every worker, without generating again.
    """
    if sys.platform == "win32":
        raise RuntimeError(
            "The recorder seed is written by Home Assistant Core, which does not "
            "run on Windows. Start the E2E suite on Linux, macOS or WSL."
        )
    import fcntl

    directory = _session_directory()
    seed = directory / "home-assistant_v2.db"
    failed = directory / "failed"
    with (directory / "lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if failed.exists():
            raise RuntimeError(failed.read_text(encoding="utf-8"))
        if not seed.exists():
            _remove_stale(directory)
            try:
                _generate(seed)
            except RuntimeError as error:
                failed.write_text(str(error), encoding="utf-8")
                raise
    return seed


def _remove_stale(current: Path) -> None:
    """Remove other sessions' directories older than _STALE_AFTER.

    No worker knows when its session ends, so none removes its own on exit.
    """
    for directory in current.parent.glob(f"{_PREFIX}*"):
        with contextlib.suppress(OSError):
            if (
                directory != current
                and time.time() - directory.stat().st_mtime > _STALE_AFTER
            ):
                shutil.rmtree(directory)


def _generate(seed: Path) -> None:
    partial = seed.with_name(f"{seed.name}.partial")
    partial.unlink(missing_ok=True)
    # The child is a session of its own, not part of the caller's.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["RECORDER_SEED_OUT"] = str(partial)
    command = [
        sys.executable,
        "-m",
        "pytest",
        _GENERATOR,
        "-p",
        "no:cacheprovider",
        "-o",
        "log_cli=false",
        # The tail reported on failure must hold the traceback.
        "--disable-warnings",
        # A hang dumps every thread's stack before the timeout kills the child.
        "-o",
        f"faulthandler_timeout={_TIMEOUT - 60}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=_TESTS_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Recorder seed generation timed out after {_TIMEOUT}s:\n"
            f"{_tail(error.output)}"
        ) from error
    if result.returncode != 0 or not partial.exists():
        raise RuntimeError(
            f"Recorder seed generation failed (exit {result.returncode}):\n"
            f"{_tail(result.stdout)}"
        )
    partial.replace(seed)


def _tail(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return "\n".join((output or "").splitlines()[-_TAIL_LINES:])
