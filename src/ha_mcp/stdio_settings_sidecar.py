"""Persistent localhost settings UI for stdio-mode installations.

Stdio MCP servers (Claude Desktop, Claude Code, default Docker) run as
short-lived subprocesses spawned by the AI client. They idle-die and
get SIGTERM'd by known client bugs, which makes the in-process HTTP
settings page used by the ``ha-mcp-web`` / add-on entrypoints
unreachable when the user wants to open it.

This module addresses that by spawning a tiny standalone Starlette
HTTP server in a detached child process on stdio startup. The child
survives parent SIGTERM / idle-death, lives until the next stdio
startup replaces it (or the OS reboots, or the user disables it), and
serves the same settings page the HTTP modes serve — the route
handlers are shared via
:func:`ha_mcp.settings_ui.build_settings_handlers` so there's no second
surface to maintain.

Security posture:
    - Bind 127.0.0.1 only (never the wildcard).
    - Random secret path (16 bytes urlsafe) generated on first spawn and
      persisted in ``ui.state`` (0600) so the settings URL stays stable
      across replace-on-startup. To rotate it, stop the sidecar (the
      page's Stop button, then remove the disable sentinel) and delete
      ``ui.state`` — while a sidecar is running, a deleted ``ui.state``
      is re-seeded from the live ``ui.url`` on the next startup.
    - Random free port chosen on first spawn and remembered the same way
      (an explicit pin, #1587, takes precedence; a remembered port that
      became unavailable falls back to a fresh ephemeral one).
    - ``Host`` header validation: rejects requests whose host doesn't
      match the bound socket — blocks DNS rebinding attacks where a
      malicious website resolves an attacker-controlled domain to
      ``127.0.0.1`` to reach this listener from the user's browser.
    - ``Origin`` validation on mutating methods.
    - ``~/.ha-mcp/`` state files (``ui.url``, ``ui.pid``, ``ui.state``,
      ``files.lock``) written 0600; ``sidecar.log`` 0644.

Disable mechanisms:
    - ``HA_MCP_DISABLE_SETTINGS_UI`` env var (truthy → skip spawn).
    - ``~/.ha-mcp/settings_ui_disabled`` sentinel file — created by the
      ``POST /shutdown`` endpoint, honored on next parent startup.

Lifecycle:
    - Parent stdio process calls :func:`maybe_spawn` shortly after
      argument validation, before entering the stdio event loop.
    - :func:`maybe_spawn` retires any previously spawned sidecar via its
      own ``POST /shutdown`` endpoint and spawns a fresh child, so the
      settings UI always reflects the running server's code and
      environment (issue #2131).
    - Child runs until replaced by the next stdio startup or killed
      (OS reboot or ``POST /shutdown``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

# Truthy values for the env-var kill switch. Inlined rather than
# importing from the tools subpackage because this module is loaded
# during early stdio startup, before any tool code is touched.
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _sidecar_dir() -> Path:
    """Return the directory used for sidecar state files.

    Routed through :func:`utils.data_paths.get_data_dir` so the sidecar
    shares the same data root as the rest of ha-mcp (respecting
    ``HA_MCP_CONFIG_DIR``, the add-on ``/data`` mount, etc.).
    """
    from .utils.data_paths import get_data_dir

    return get_data_dir()


def _url_file() -> Path:
    return _sidecar_dir() / "ui.url"


def _pid_file() -> Path:
    return _sidecar_dir() / "ui.pid"


def _log_file() -> Path:
    return _sidecar_dir() / "sidecar.log"


def _disabled_sentinel() -> Path:
    return _sidecar_dir() / "settings_ui_disabled"


def _state_file() -> Path:
    return _sidecar_dir() / "ui.state"


def _load_sidecar_state() -> tuple[int, str] | None:
    """Return ``(port, secret_path)`` persisted by a prior spawn, or None.

    ``ui.url``/``ui.pid`` mean "a sidecar is serving right now" and die
    with the process; ``ui.state`` means "this install's stable URL" and
    outlives it. With replace-on-startup (issue #2131) the process no
    longer survives restarts, so URL stability comes from rebinding the
    remembered port and reusing the remembered secret path.

    Both values are validated strictly — the secret is interpolated into
    route paths, so a corrupted file must yield None (fresh values), not
    a malformed route table.
    """
    try:
        text = _state_file().read_text()
    except FileNotFoundError:
        return None  # normal first spawn — nothing to say
    except OSError:
        logger.warning(
            "Cannot read %s; generating a fresh settings URL.",
            _state_file(),
            exc_info=True,
        )
        return None

    def _invalid(reason: str) -> None:
        # A present-but-invalid file means the URL is about to change —
        # exactly the symptom this state exists to prevent — so say why.
        logger.warning(
            "Invalid ui.state (%s); generating a fresh settings URL.", reason
        )

    try:
        raw = json.loads(text)
    except ValueError:
        _invalid("not JSON")
        return None
    if not isinstance(raw, dict):
        _invalid("non-object root")
        return None
    port = raw.get("port")
    secret = raw.get("secret_path")
    # Floor matches the sidecar_pin_port validator: no privileged ports,
    # and real ephemeral ports never sit below 1024 anyway.
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        _invalid(f"port {port!r} out of range")
        return None
    if not isinstance(secret, str) or not re.fullmatch(
        r"/private_[A-Za-z0-9_-]+", secret
    ):
        _invalid("malformed secret_path")
        return None
    return port, secret


def _save_sidecar_state(port: int, secret_path: str) -> None:
    """Persist the bound port + secret for the next spawn (best effort)."""
    try:
        _atomic_write_0600(
            _state_file(),
            json.dumps({"port": port, "secret_path": secret_path}) + "\n",
        )
    except OSError:
        logger.warning(
            "Failed to persist sidecar state at %s; the settings URL will "
            "change on the next restart.",
            _state_file(),
            exc_info=True,
        )


def read_sidecar_url() -> str | None:
    """Return the current sidecar URL, or None if no sidecar is running.

    Reads ``~/.ha-mcp/ui.url`` if present. Consumed by
    ``ha_get_overview`` to surface the URL to the LLM (and through it,
    the user) on every overview call.
    """
    try:
        return _url_file().read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Cannot read sidecar URL file", exc_info=True)
        return None


def _is_disabled() -> bool:
    """Check whether the sidecar should be skipped."""
    val = os.environ.get("HA_MCP_DISABLE_SETTINGS_UI", "").strip().lower()
    if val in _TRUTHY:
        return True
    if val and val not in _FALSY:
        # Fail closed on an unrecognized value (same semantics as the HTTP
        # transports' _settings_ui_disabled), and warn so the operator sees why
        # the settings UI never came up.
        logger.warning(
            "HA_MCP_DISABLE_SETTINGS_UI=%r is not a recognized on/off value; "
            "skipping the settings-UI sidecar (fail-closed).",
            val,
        )
        return True
    return _disabled_sentinel().exists()


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows: there's no kill(0) equivalent. OpenProcess is the
        # canonical check but pulls in pywin32; for a best-effort
        # liveness probe we use the cheaper tasklist exit code via
        # ctypes.windll.kernel32 (no external deps).
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user / different
        # security context — treat as "another instance is alive,
        # don't double-spawn".
        return True
    return True


def _read_recorded_pid() -> int | None:
    """Return the PID recorded in ``ui.pid``, or None if absent/garbage."""
    try:
        return int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


# Timeouts for retiring the previous sidecar. The HTTP timeout is
# urllib's per-socket-operation timeout (a listener that accepts and
# dribbles can stretch the total), which in practice bounds the startup
# delay when the recorded URL points at a hung process; the exit wait
# bounds how long we give a healthy sidecar to finish dying.
_SHUTDOWN_HTTP_TIMEOUT = 2.0
_OLD_SIDECAR_EXIT_WAIT = 5.0
_OLD_SIDECAR_EXIT_POLL = 0.1
# How long the spawning parent keeps the spawn lock while the child gets
# through interpreter start + imports to _write_pid_url.
_CHILD_PUBLISH_WAIT = 5.0


def _no_proxy_opener() -> Any:
    """Opener that never routes through HTTP(S)_PROXY.

    The shutdown POST targets 127.0.0.1 and embeds the secret path;
    urllib honors environment proxies even for loopback unless
    ``no_proxy`` says otherwise, which would both leak the secret to the
    proxy and never reach the listener.
    """
    import urllib.request

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Shape run_main() writes into ui.url. Doubles as the legacy-migration
# parser: pre-ui.state releases used the same format.
_SIDECAR_URL_RE = re.compile(
    r"http://127\.0\.0\.1:(\d{1,5})(/private_[A-Za-z0-9_-]+)/settings"
)


def _seed_state_from_url(url: str) -> None:
    """Migrate a pre-``ui.state`` install's stable URL into the state file.

    Releases before the replace-on-startup fix kept the port and secret
    only in ``ui.url``, which the replace flow deletes. Without this
    seed, the first upgraded startup would mint a fresh URL and break
    every existing bookmark — the exact thing the persisted state exists
    to prevent. No-op when ``ui.state`` already holds valid state (it is
    authoritative) or the URL doesn't parse.
    """
    if _load_sidecar_state() is not None:
        return
    match = _SIDECAR_URL_RE.fullmatch(url.strip())
    if match is None:
        return
    port = int(match.group(1))
    if not 1024 <= port <= 65535:  # same floor as _load_sidecar_state
        return
    _save_sidecar_state(port, match.group(2))


def _shutdown_url(url: str) -> str:
    """Derive the retire POST target from a recorded settings URL.

    Shared between the retire flow and its producer/consumer test — the
    arithmetic must stay in lockstep with the route table in
    :func:`_build_app`. ``mode=retire`` tells a current-code endpoint to
    skip the disable-sentinel write (a retire is not a disable), so
    every sentinel on disk is user-owned; legacy endpoints ignore the
    parameter and keep their write-then-clear contract.
    """
    return url.removesuffix("/settings") + "/api/settings/shutdown?mode=retire"


def _post_shutdown(url: str) -> tuple[bool, bool | None]:
    """POST the retire request; returns ``(acked, sentinel_created)``.

    ``acked`` is True only when the old sidecar acknowledged (2xx).
    ``sentinel_created`` reports whether THIS request created the disable
    sentinel (None when the endpoint predates the field). Every failure
    mode gets its own truthful log line — a refusal (HTTPError: the
    endpoint answered "alive and NOT shutting down") and a timeout
    (alive but slow) are NOT "no responsive sidecar", and both return
    acked=False so the caller neither clears the sentinel nor waits on
    a pid that will not exit.
    """
    import urllib.error

    try:
        import urllib.request

        request = urllib.request.Request(_shutdown_url(url), data=b"", method="POST")
        with _no_proxy_opener().open(request, timeout=_SHUTDOWN_HTTP_TIMEOUT) as resp:
            # urllib raises for non-2xx; reaching here is success. The
            # endpoint reports whether THIS request created the disable
            # sentinel — the authoritative signal for clearing it (a
            # user's Stop can land between the caller's snapshot and this
            # POST). Legacy endpoints without the field yield None and
            # the caller falls back to its snapshot.
            sentinel_created: bool | None = None
            with contextlib.suppress(OSError, ValueError, TypeError):
                payload = json.loads(resp.read())
                if isinstance(payload, dict) and isinstance(
                    payload.get("sentinel_created"), bool
                ):
                    sentinel_created = payload["sentinel_created"]
            return True, sentinel_created
    except urllib.error.HTTPError as exc:
        # Caught before URLError, which it subclasses. The endpoint's
        # non-2xx answers (sentinel write failed / stop() raised) leave
        # the process running and no sentinel behind.
        logger.warning(
            "Old sidecar refused shutdown (HTTP %s); spawning replacement anyway.",
            exc.code,
        )
    except TimeoutError as exc:
        _log_shutdown_timeout(exc)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            _log_shutdown_timeout(exc.reason)
        else:
            logger.info(
                "No responsive sidecar at the recorded URL (%s); "
                "replacing state files only.",
                exc,
            )
    except (OSError, ValueError) as exc:
        # ValueError: a corrupt ui.url ("garbage") fails Request
        # construction before any I/O — still just stale state to replace.
        logger.info(
            "No responsive sidecar at the recorded URL (%s); "
            "replacing state files only.",
            exc,
        )
    return False, None


def _log_shutdown_timeout(exc: BaseException) -> None:
    logger.warning(
        "Old sidecar did not answer /shutdown within %.1fs (%s); "
        "it may still be running. Spawning replacement anyway.",
        _SHUTDOWN_HTTP_TIMEOUT,
        exc,
    )


def _shutdown_existing_sidecar() -> None:
    """Retire a previously spawned sidecar so a fresh one can replace it.

    Reusing a running sidecar froze the settings UI at the code and
    environment of whatever parent spawned it first — in issue #2131 a
    57-day-old orphan from a long-gone install kept serving stale feature
    flags through many client restarts and upgrades. Every stdio startup
    therefore retires the old sidecar and spawns its own.

    Termination goes through the sidecar's own ``POST /shutdown`` endpoint
    on the recorded secret-path URL, never ``os.kill``:

    * Only the real sidecar was ever told the secret path, so a listener
      answering on it is ours for practical purposes (a recycled port
      would need a catch-all handler to false-positive) — and even then
      nothing is killed: the worst a stranger receives is a POST it
      ignores.
    * The endpoint has lived at the same path since the first sidecar
      release (#1381), so orphans spawned by any past version are retired
      too.

    Best-effort throughout: on any failure the caller still spawns the
    replacement — worst case an unresponsive old process lingers until
    reboot, but the discovery files are unlinked and rewritten by the
    replacement, so nothing hands out the old URL anymore (if the
    replacement loses the port to that lingering process, it serves on
    an ephemeral fallback instead).
    """
    url = read_sidecar_url()
    if url is None:
        return
    _seed_state_from_url(url)

    # Snapshot BEFORE the POST: /shutdown writes the disable sentinel, so
    # afterwards its presence is ambiguous between "my POST caused it"
    # (clear it — this is a replace, not a disable) and "the user clicked
    # Stop in this very window" (keep it — their disable must stick).
    sentinel_preexisting = _disabled_sentinel().exists()

    acked, sentinel_created = _post_shutdown(url)
    if not acked:
        return
    logger.info("Retired previous settings UI sidecar at %s", url)

    # /shutdown drops the disable sentinel before signalling exit — its
    # "user clicked shutdown" contract. This is a replace, not a disable:
    # clear the sentinel or the freshly spawned child would honor it and
    # exit immediately. Only a sentinel our own POST caused is cleared —
    # the endpoint's sentinel_created field is authoritative (it closes
    # the window where a user's Stop lands between the snapshot above
    # and our POST); legacy endpoints without the field fall back to
    # the snapshot.
    should_clear = (
        sentinel_created if sentinel_created is not None else not sentinel_preexisting
    )
    if should_clear:
        with contextlib.suppress(FileNotFoundError, OSError):
            _disabled_sentinel().unlink()

    pid = _read_recorded_pid()
    if pid is None:
        return

    import time

    # Wait (bounded) for the old process to exit, for two reasons: (a) it
    # holds the port the replacement wants to rebind — the sticky URL only
    # survives if the port frees inside this window; (b) sidecars from
    # releases BEFORE the ownership-guarded cleanup unlink ui.pid/ui.url
    # blindly on exit, so during migration a still-dying legacy process
    # could delete the replacement's freshly written files. (Current-code
    # sidecars can't: _cleanup_owned_serving_files checks ownership under
    # _serving_files_lock.)
    deadline = time.monotonic() + _OLD_SIDECAR_EXIT_WAIT
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            logger.warning(
                "Old sidecar pid %s still alive %.0fs after acknowledging "
                "shutdown; spawning replacement anyway.",
                pid,
                _OLD_SIDECAR_EXIT_WAIT,
            )
            return
        time.sleep(_OLD_SIDECAR_EXIT_POLL)


def _spawn_lock_path() -> Path:
    return _sidecar_dir() / "spawn.lock"


@contextlib.contextmanager
def _spawn_lock() -> Iterator[bool]:
    """Yield True if this caller holds the spawn lock, False if another holds it.

    Serializes concurrent ``maybe_spawn()`` calls so two parent stdio
    processes starting in rapid succession can't both run the
    retire-and-respawn window — the loser's child would race on
    ``bind()`` and crash into ``sidecar.log``.

    Non-blocking: a caller that can't acquire the lock returns False
    immediately and the parent should skip spawning (the holding
    parent is doing it). Released on context exit. Lock file lives at
    ``~/.ha-mcp/spawn.lock`` (mode 0o600).

    Falls back to no-op (yields True) if the OS-specific lock primitive
    isn't available or fails — better to risk the rare race than to
    refuse to spawn at all on an exotic platform.
    """
    lock_path = _spawn_lock_path()
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        logger.debug(
            "Cannot open spawn lock file %s; proceeding unlocked",
            lock_path,
            exc_info=True,
        )
        yield True
        return

    try:
        if sys.platform == "win32":
            try:
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            except OSError:
                logger.debug(
                    "fcntl.flock failed on %s; proceeding unlocked",
                    lock_path,
                    exc_info=True,
                )
                yield True
                return
            try:
                yield True
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _bind_listener(preferred: int, source: str) -> socket.socket:
    """Bind and return the sidecar's actual serving socket.

    Binding the real socket up front (instead of probing a port and
    letting uvicorn rebind it) makes port acquisition atomic: a busy
    preferred port fails HERE, before any state is persisted or
    discovery file written, and falls back to an ephemeral port.

    Platform flags matter (#2134 review):

    * Windows: ``SO_EXCLUSIVEADDRUSE``. Plain ``SO_REUSEADDR`` there
      permits binding over a live listener that also set it — the probe
      would report the busy port as free and the child would die inside
      uvicorn's real bind after the discovery files were written.
    * POSIX: ``SO_REUSEADDR``, keeping the ``TIME_WAIT`` grace a
      pinned/remembered port needs across quick restarts (#1587).

    ``source`` names the caller's intent ("Pinned" or "Remembered") so
    the fallback warning doesn't blame a pin the user never set.
    Raises OSError only when even the ephemeral bind fails — the caller
    must exit without touching any state file.
    """
    candidates = [preferred, 0] if preferred else [0]
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,  # type: ignore[attr-defined,unused-ignore]
                    1,
                )
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", candidate))
        except OSError as exc:
            sock.close()
            if candidate == 0:
                raise
            logger.warning(
                "%s sidecar port %d unavailable (%s); "
                "falling back to an ephemeral port",
                source,
                candidate,
                exc,
            )
            continue
        return sock
    raise OSError("no bind candidates")  # unreachable: [.., 0] always ends in raise


def maybe_spawn(prepare: Callable[[], None] | None = None) -> None:
    """Retire any previous sidecar and spawn a fresh one.

    Called once from stdio ``main()`` after argument validation. No-op
    when the sidecar is disabled (env var or sentinel), when a
    concurrent parent already holds the spawn lock, or when subprocess
    spawn raises (best effort; the MCP server continues regardless).

    A previously spawned sidecar is never reused (issue #2131): it
    serves the code and environment of the parent that spawned it, which
    may be a long-dead install many versions old. Replacing it on every
    startup keeps the settings UI in lockstep with this server process.

    ``prepare`` runs winner-only, inside the spawn lock, just before the
    child is launched — the metadata-cache dump goes here so a lock
    LOSER (whose environment may differ) can never overwrite the cache
    the winner's sidecar serves.
    """
    if _is_disabled():
        logger.info(
            "Settings UI sidecar disabled (env var or %s sentinel); skipping spawn.",
            _disabled_sentinel().name,
        )
        return

    # Serialize concurrent spawn attempts. Two parent stdio processes
    # starting in rapid succession (e.g. user launching Claude Desktop
    # then Claude Code back-to-back) could both run the retire + Popen
    # window — the loser's child would race on bind() and die into
    # sidecar.log. The lock ensures only one parent runs it at a time;
    # the loser skips (and reports the winner's URL below if it is
    # already published — the winner may still be mid-spawn).
    with _spawn_lock() as acquired:
        if not acquired:
            logger.info(
                "Another parent process is currently spawning the sidecar; skipping."
            )
            existing = read_sidecar_url()
            if existing:
                print(f"ha-mcp settings UI at: {existing}", file=sys.stderr)
            return

        # prepare (the heavy metadata dump — a full server build) runs
        # BEFORE the retire: the old sidecar keeps serving, and keeps the
        # remembered port bound, while the slow work happens. The port's
        # unbound window shrinks to spawn-plus-child-startup.
        if prepare is not None:
            try:
                prepare()
            except Exception:
                # The cache dump is advisory; a failure must not cost the
                # user their settings UI.
                logger.warning(
                    "Sidecar prepare hook failed; spawning anyway.", exc_info=True
                )
        _shutdown_existing_sidecar()
        _do_spawn()


def _do_spawn() -> None:
    """Inner spawn; caller holds the spawn lock, predecessor retired.

    Extracted from :func:`maybe_spawn` so the context manager doesn't
    indent the full Popen block.
    """
    # Clean stale pid/url files from a previous crash before spawning.
    for stale in (_pid_file(), _url_file()):
        with contextlib.suppress(FileNotFoundError, OSError):
            stale.unlink()

    log_path = _log_file()
    try:
        log_handle = log_path.open("a", buffering=1)
    except OSError:
        logger.warning(
            "Cannot open sidecar log file %s; sidecar output will be lost",
            log_path,
            exc_info=True,
        )
        log_handle = None

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle if log_handle is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # Three-layer defense against the cmd-window-pops-and-closing-
        # it-kills-the-server bug reported against Claude Desktop:
        #
        # 1. Prefer ``pythonw.exe`` over ``python.exe``. pythonw is the
        #    GUI-subsystem Python — Windows never allocates a console
        #    for it. Falls back to python.exe if pythonw isn't there
        #    (uv-installed Pythons sometimes strip it).
        # 2. ``STARTUPINFO`` with ``SW_HIDE`` forces any console that
        #    DOES get allocated to be hidden. Catches the fallback case.
        # 3. ``CREATE_NO_WINDOW`` suppresses console allocation entirely
        #    for the python.exe fallback. ``CREATE_NEW_PROCESS_GROUP``
        #    prevents a CTRL_CLOSE_EVENT (X-button on any console that
        #    sneaks through) from killing the sidecar.
        #
        # Why not DETACHED_PROCESS: prior attempts used it, but Python's
        # docs spell out that ``CREATE_NO_WINDOW`` is IGNORED when
        # DETACHED_PROCESS is set, and DETACHED_PROCESS on a CUI binary
        # auto-allocates a fresh visible console — the exact failure
        # mode the user saw.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        cmd_python = str(pythonw) if pythonw.exists() else sys.executable
        startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined,unused-ignore]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined,unused-ignore]
        startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined,unused-ignore]
        popen_kwargs["startupinfo"] = startupinfo
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined,unused-ignore]
        )
        cmd = [cmd_python, "-m", "ha_mcp.stdio_settings_sidecar"]
    else:
        # New session leader → parent SIGTERM / shell exit doesn't
        # cascade. Detaches from the parent's session so SIGHUP /
        # terminal close doesn't follow into the child. The full
        # daemonize sequence (double-fork to drop the controlling TTY)
        # isn't needed here since the parent stdio process already has
        # no TTY to inherit.
        popen_kwargs["start_new_session"] = True
        cmd = [sys.executable, "-m", "ha_mcp.stdio_settings_sidecar"]
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except OSError:
        logger.warning(
            "Failed to spawn settings UI sidecar (%s); MCP server continues without it.",
            " ".join(cmd),
            exc_info=True,
        )
        if log_handle is not None:
            log_handle.close()
        return

    # The child writes its own pid/url file once it's bound — but log the
    # spawn here so users grepping the parent stderr see something
    # immediately, even before the child reports back.
    logger.info(
        "Spawned settings UI sidecar pid=%d (log: %s). URL written to %s once ready.",
        proc.pid,
        log_path,
        _url_file(),
    )
    print(
        f"ha-mcp settings UI sidecar spawned (pid {proc.pid}). "
        f"URL will be written to {_url_file()}",
        file=sys.stderr,
    )

    # Keep the spawn lock (held by our caller) until the child has
    # published its URL. The child needs a full interpreter start before
    # _write_pid_url runs; a second parent starting inside that window
    # would find no ui.url, retire nothing, and spawn a duplicate child
    # against the same remembered port. Holding the lock makes that
    # parent take the loser path instead. Bounded: an import crash in
    # the child must not wedge stdio startup.
    import time

    deadline = time.monotonic() + _CHILD_PUBLISH_WAIT
    while not _url_file().exists():
        if proc.poll() is not None:
            logger.warning(
                "Settings UI sidecar pid=%d exited (rc=%s) before publishing "
                "its URL; see %s.",
                proc.pid,
                proc.returncode,
                log_path,
            )
            return
        if time.monotonic() >= deadline:
            logger.warning(
                "Settings UI sidecar pid=%d has not published its URL after "
                "%.0fs; continuing without waiting further.",
                proc.pid,
                _CHILD_PUBLISH_WAIT,
            )
            return
        time.sleep(_OLD_SIDECAR_EXIT_POLL)


# --------------------------------------------------------------------------
# Sidecar child process — runs when this module is invoked via
# ``python -m ha_mcp.stdio_settings_sidecar``.
# --------------------------------------------------------------------------


def _atomic_write_0600(path: Path, content: str) -> None:
    """Create ``path`` with 0o600 perms atomically and write ``content``.

    Two atomicity guarantees, both needed:

    1. **Perms**: ``Path.write_text()`` opens with default perms (0o644
       under a typical 022 umask) and only restricts them via a
       follow-up ``os.chmod`` — a TOCTOU window where the URL (a
       credential — it embeds the secret path) is briefly
       world-readable on shared hosts. ``os.open`` with an explicit
       ``mode`` arg sets perms on the creating syscall itself, closing
       the window.

    2. **Content**: write to a sibling ``<path>.tmp`` and ``os.replace``
       into place. ``O_TRUNC`` directly on ``path`` would leave an
       empty/truncated file if the writer dies mid-write (signal, OOM,
       disk full) — and a half-written URL file next to a still-live
       PID file produces the worst-possible state (consumers read
       ``None`` and assume "no sidecar" even though one is running).
       ``os.replace`` is atomic on POSIX and on Windows (since Vista),
       so readers always see either the old content or the full new
       content.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fp:
            fp.write(content)
        os.replace(tmp, path)
    except BaseException:
        # On any failure (including KeyboardInterrupt) clean up the
        # tmp file so a retry doesn't trip over a stale leftover.
        with contextlib.suppress(FileNotFoundError, OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _serving_files_lock() -> Iterator[None]:
    """Serialize ui.pid/ui.url writes against exit cleanup across processes.

    A successor writing its serving files can interleave with its
    predecessor's exit cleanup; without a lock the predecessor can pass
    its ownership check and then unlink files the successor wrote a
    moment later. Both critical sections are tiny, so this blocks
    (unlike ``_spawn_lock``); on any lock failure it degrades to
    unlocked best-effort rather than blocking sidecar exit or startup.
    """
    lock_path = _sidecar_dir() / "files.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        # Warning, not debug: unlocked means the predecessor-vs-successor
        # file race this lock exists to prevent is back — worth a trace
        # in sidecar.log when it happens (e.g. flock-less network mounts).
        logger.warning("Cannot open %s; proceeding unlocked", lock_path, exc_info=True)
        yield
        return
    locked = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                # LK_LOCK retries for ~10s before raising — an effective
                # bounded blocking wait.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        except OSError:
            logger.warning(
                "Serving-files lock unavailable; proceeding unlocked "
                "(predecessor/successor file writes are unserialized)",
                exc_info=True,
            )
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_pid_url(url: str) -> None:
    """Persist the sidecar URL and pid for parent / overview consumption.

    Writes pid BEFORE url — the ordering is load-bearing twice over:
    a partial failure can't leave a URL file pointing at a dead port
    without the matching pid file, and ``_cleanup_owned_serving_files``
    relies on it ("pid is mine" implies a successor hasn't started
    writing, since a successor lands its pid first). If the URL write
    fails after the pid file lands, both are removed rather than leaving
    the pair inconsistent.

    Runs under ``_serving_files_lock`` so a slow-exiting predecessor's
    cleanup can't interleave with these writes.
    """
    with _serving_files_lock():
        url_path = _url_file()
        pid_path = _pid_file()
        try:
            _atomic_write_0600(pid_path, f"{os.getpid()}\n")
        except OSError:
            logger.exception("Failed to write sidecar pid file at %s", pid_path)
            return
        try:
            _atomic_write_0600(url_path, url + "\n")
        except OSError:
            logger.exception("Failed to write sidecar URL file at %s", url_path)
            # Roll back the pid write so the next maybe_spawn() doesn't think
            # there's a live sidecar with an unreadable URL.
            with contextlib.suppress(FileNotFoundError, OSError):
                pid_path.unlink()


# Note: a custom ``_install_shutdown_handlers`` lived here previously.
# Removed because uvicorn's ``Server.run()`` already installs SIGTERM /
# SIGINT handlers that set ``should_exit = True`` — the same behavior
# the custom handler provided. The /shutdown HTTP endpoint reaches the
# same stop callable via ``app.state.shutdown_state`` and is unaffected.


def _build_shutdown_handler(
    shutdown_lock: threading.Lock,
    shutdown_state: dict[str, Callable[[], None] | None],
) -> Callable[[Request], Awaitable[Any]]:
    """Build the POST /shutdown handler.

    Two callers, two contracts. The page's Stop button (no mode param)
    is a DISABLE: drop the sentinel BEFORE signalling exit so a fast
    restart cycle doesn't race past the check in maybe_spawn(); if the
    sentinel write fails, surface it and keep running. The replace
    flow's ``mode=retire`` is NOT a disable: it writes no sentinel at
    all, so every sentinel on disk is user-owned and the replace flow
    never has one of its own to clear — a user Stop landing anywhere
    around a retire always sticks. ``sentinel_created`` reports what
    THIS request did (requests are serialized on uvicorn's single event
    loop, so the exists-then-write pair cannot interleave).
    """
    from starlette.responses import JSONResponse

    def _write_sentinel_or_error() -> Any | None:
        try:
            _disabled_sentinel().write_text(
                f"Disabled via /shutdown endpoint at pid {os.getpid()}\n"
            )
        except OSError as e:
            logger.exception("Failed to write disabled sentinel")
            return JSONResponse(
                {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": (
                            f"Failed to write disable sentinel "
                            f"({type(e).__name__}: {e}); sidecar not shutting "
                            "down. Set HA_MCP_DISABLE_SETTINGS_UI=1 and "
                            "restart your MCP client to disable."
                        ),
                    },
                },
                status_code=500,
            )
        return None

    async def _shutdown_endpoint(request: Request) -> Any:
        is_retire = request.query_params.get("mode") == "retire"
        sentinel_created = False
        if not is_retire:
            sentinel_created = not _disabled_sentinel().exists()
            error = _write_sentinel_or_error()
            if error is not None:
                return error
        with shutdown_lock:
            stop = shutdown_state.get("stop")
        if stop is not None:
            try:
                stop()
            except Exception as stop_exc:
                # Sentinel write already succeeded; if we now report
                # "shutting down" but the process keeps running, the
                # user has the worst possible state: UI loads on this
                # session, but next restart skips spawning. Roll back
                # the sentinel so subsequent ha-mcp launches still
                # spawn the sidecar, and surface the failure. Only a
                # sentinel THIS request created may be rolled back — in
                # retire mode (or a repeat Stop) any sentinel on disk is
                # someone else's disable.
                logger.exception("uvicorn stop() raised — rolling back sentinel")
                if sentinel_created:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        _disabled_sentinel().unlink()
                return JSONResponse(
                    {
                        "success": False,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": (
                                f"Sentinel written but server stop failed "
                                f"({type(stop_exc).__name__}: {stop_exc}); "
                                "sentinel was rolled back so future "
                                "launches will still spawn. Set "
                                "HA_MCP_DISABLE_SETTINGS_UI=1 and "
                                "restart your MCP client to disable."
                            ),
                        },
                    },
                    status_code=500,
                )
        # The re-enable instruction only makes sense for a disable — a
        # retire wrote no sentinel and is followed by a replacement.
        message = (
            "Settings UI sidecar shutting down for replacement."
            if is_retire
            else (
                "Settings UI sidecar shutting down. "
                f"Delete {_disabled_sentinel()} to re-enable on next ha-mcp start."
            )
        )
        return JSONResponse(
            {
                "success": True,
                "sentinel_created": sentinel_created,
                "message": message,
            }
        )

    return _shutdown_endpoint


def _build_app(
    host: str,
    port: int,
    secret_path: str,
) -> Any:
    """Construct the Starlette app the sidecar serves.

    Imported lazily — the parent process should not pay for Starlette
    import at MCP startup time (it's already paid by the FastMCP
    transports, but stdio mode shouldn't be).
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from .settings_ui import build_settings_handlers

    handlers = build_settings_handlers(server=None, is_sidecar=True)

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    mutating_methods = {"POST", "PUT", "DELETE", "PATCH"}

    class SecurityMiddleware(BaseHTTPMiddleware):
        """DNS-rebinding and CSRF guards.

        DNS rebinding: a malicious site sets up an A record that points
        first at its own IP (passing same-origin checks for the
        attacker page's JS) then re-resolves to 127.0.0.1, so the
        attacker's JS can fetch this listener. The browser still sends
        the attacker's hostname in the ``Host`` header — rejecting any
        Host that isn't 127.0.0.1 / localhost (on the bound port) blocks
        this without touching the network stack.

        CSRF: a malicious page can issue cross-origin POSTs from the
        user's browser. ``Origin`` is set by every modern browser on
        cross-origin requests and is not forgeable from JS — rejecting
        mutating methods whose ``Origin`` doesn't match the listener
        blocks the attack.
        """

        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            host_header = request.headers.get("host", "")
            if host_header not in allowed_hosts:
                logger.warning(
                    "Rejecting request with Host=%r (allowed: %s)",
                    host_header,
                    sorted(allowed_hosts),
                )
                return PlainTextResponse("Host header not allowed", status_code=400)
            if request.method in mutating_methods:
                origin = request.headers.get("origin")
                if origin is not None and origin not in allowed_origins:
                    logger.warning(
                        "Rejecting %s with Origin=%r (allowed: %s)",
                        request.method,
                        origin,
                        sorted(allowed_origins),
                    )
                    return PlainTextResponse("Origin not allowed", status_code=403)
            response = await call_next(request)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            return response

    secret_prefix = secret_path.rstrip("/")

    routes = [
        # Mount under the secret path; the user's bookmark / overview URL
        # includes it. A bare GET on / returns a generic 404 because the
        # only way to reach the page is via the secret path.
        Route(
            f"{secret_prefix}/settings",
            handlers["settings_page"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/tools",
            handlers["get_tools"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/tools",
            handlers["save_tools"],
            methods=["POST"],
        ),
        Route(
            f"{secret_prefix}/api/settings/info",
            handlers["settings_info"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/features",
            handlers["get_feature_flags"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/features",
            handlers["save_feature_flags"],
            methods=["POST"],
        ),
        # Theme / accessibility prefs (#1574 review). The sidecar is the
        # very mode these exist for: its port (= the localStorage origin)
        # is stable by default since #2131 but still changes on first
        # spawn, a lost ui.state, a pin change, or a taken remembered
        # port — and each change is a fresh empty origin the server-side
        # copy re-seeds with the user's choices.
        Route(
            f"{secret_prefix}/api/settings/theme",
            handlers["get_theme_prefs"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/theme",
            handlers["save_theme_prefs"],
            methods=["POST"],
        ),
        # Custom filesystem directories (issue #1567). The sub-form is rendered
        # in the features panel, so the stdio sidecar must serve these too — its
        # route list is hand-maintained and does NOT derive from
        # register_settings_routes. The handler builds a transient HA client
        # from the inherited env when server is None (sidecar mode).
        Route(
            f"{secret_prefix}/api/settings/fs-custom-paths",
            handlers["get_fs_custom_paths"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/settings/fs-custom-paths",
            handlers["save_fs_custom_paths"],
            methods=["POST"],
        ),
        # Tool security policies endpoints (#966). Pending/approve/deny
        # are wired as stubs that return 503 in sidecar mode — the
        # in-memory ApprovalQueue lives in the main server process, so
        # only config GET/PUT are usefully reachable here.
        Route(
            f"{secret_prefix}/api/policy/config",
            handlers["policy_get_config"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/policy/config",
            handlers["policy_put_config"],
            methods=["PUT"],
        ),
        Route(
            f"{secret_prefix}/api/policy/pending",
            handlers["policy_get_pending"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/policy/approve",
            handlers["policy_post_approve"],
            methods=["POST"],
        ),
        Route(
            f"{secret_prefix}/api/policy/deny",
            handlers["policy_post_deny"],
            methods=["POST"],
        ),
        Route(
            f"{secret_prefix}/api/policy/tool-schema",
            handlers["policy_get_tool_schema"],
            methods=["GET"],
        ),
        Route(
            f"{secret_prefix}/api/policy/value-source",
            handlers["policy_get_value_source"],
            methods=["GET"],
        ),
    ]

    # /shutdown — POST endpoint that drops the disable sentinel and
    # signals uvicorn to exit. The handler is wired in run_main() after
    # the server is constructed because we need a handle to the server
    # object — the ``shutdown_state`` dict is the indirection layer.
    shutdown_lock = threading.Lock()
    shutdown_state: dict[str, Callable[[], None] | None] = {"stop": None}
    _shutdown_endpoint = _build_shutdown_handler(shutdown_lock, shutdown_state)

    routes.append(
        Route(
            f"{secret_prefix}/api/settings/shutdown",
            _shutdown_endpoint,
            methods=["POST"],
        )
    )

    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware)],
    )
    # Stash the shutdown_state on the app so run_main() can install the
    # uvicorn stop callable into it once the server is built.
    app.state.shutdown_state = shutdown_state
    app.state.shutdown_lock = shutdown_lock
    return app


def run_main() -> int:
    """Sidecar entry point — invoked via ``python -m ha_mcp.stdio_settings_sidecar``.

    Resolves the port and secret path (persisted values from a prior
    spawn when available, fresh ones otherwise), writes pid+url files,
    and runs uvicorn until killed. Returns the exit code.
    """
    # Honor the disable sentinel on direct invocation too, so a user
    # who disabled via /shutdown but later tried to start the sidecar
    # manually still gets the configured behavior. Checked before any
    # heavy import (e.g. uvicorn) so the disable path is fast and
    # doesn't pay the uvicorn-import cost.
    if _is_disabled():
        print(
            "Settings UI sidecar disabled (env var or sentinel). "
            f"Delete {_disabled_sentinel()} to re-enable.",
            file=sys.stderr,
        )
        return 0

    import uvicorn

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Effective pin port honours both the env var (HA_MCP_SIDECAR_PORT) and
    # a value set via the settings UI Advanced tab (persisted to the override
    # file and applied by get_global_settings). The lenient validator in
    # config.Settings has already clamped any bad value to 0. With no pin,
    # the port + secret persisted by a prior spawn are reused so the
    # settings URL survives replace-on-startup (issue #2131); only a truly
    # first spawn (or a lost/invalid ui.state) picks fresh values.
    from .config import get_global_settings

    persisted = _load_sidecar_state()
    remembered_port, remembered_secret = persisted if persisted else (0, None)
    pin_port = get_global_settings().sidecar_pin_port
    try:
        listener = _bind_listener(
            pin_port or remembered_port, "Pinned" if pin_port else "Remembered"
        )
    except OSError:
        # Not even an ephemeral port could be bound. This child can never
        # serve — exit WITHOUT touching ui.state or the discovery files,
        # which may belong to a live predecessor.
        logger.exception("Cannot bind any port for the settings UI; exiting.")
        return 1
    port = int(listener.getsockname()[1])
    if remembered_secret is None:
        secret_path = f"/private_{secrets.token_urlsafe(16)}"
    else:
        secret_path = remembered_secret
    url = f"http://127.0.0.1:{port}{secret_path}/settings"
    # Persist only AFTER the successful bind: the state must describe a
    # port this process actually serves, or the next spawn inherits a
    # port that doesn't match the URL the user bookmarked.
    _save_sidecar_state(port, secret_path)

    app = _build_app(host="127.0.0.1", port=port, secret_path=secret_path)

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        # Bounded graceful drain keeps a retiring sidecar's exit inside
        # the parent's _OLD_SIDECAR_EXIT_WAIT window.
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)

    def _stop() -> None:
        server.should_exit = True

    # Wire the stop callable into app state so the /shutdown HTTP
    # endpoint can reach it. OS signals (SIGTERM / SIGINT) are handled
    # by uvicorn's own ``Server.run()`` installation — see the note
    # earlier in this module for why we don't install our own.
    with app.state.shutdown_lock:
        app.state.shutdown_state["stop"] = _stop

    # Start listening BEFORE publishing the URL: a parent that reads the
    # fresh ui.url may POST /shutdown immediately, and a bound-but-not-
    # listening socket would refuse it — misread as "no sidecar" and
    # answered with a duplicate spawn. Listening here lets the kernel
    # queue that connection until uvicorn's accept loop takes over
    # (asyncio's create_server re-listens on an already-listening socket
    # without complaint).
    listener.listen(128)

    _write_pid_url(url)

    logger.info("Settings UI sidecar listening at %s", url)
    print(f"ha-mcp settings UI ready at: {url}", file=sys.stderr)

    try:
        # Hand uvicorn the pre-bound socket — re-binding host/port here
        # would reintroduce the probe-then-rebind race _bind_listener
        # exists to close.
        server.run(sockets=[listener])
    finally:
        # Best-effort cleanup of serving files on graceful exit — but only
        # the ones this process still owns. The retire wait in
        # _shutdown_existing_sidecar is bounded, so a slow exit can outlive
        # its replacement's file writes; a blind unlink here would delete
        # the NEW sidecar's discovery files. (ui.state is deliberately
        # never cleaned: it carries the stable URL to the next spawn.)
        _cleanup_owned_serving_files()

    return 0


def _cleanup_owned_serving_files() -> None:
    """Unlink ui.pid/ui.url iff ui.pid still records THIS process.

    The pid is the only valid ownership token: two live processes never
    share one, while with sticky ``ui.state`` a successor's ui.url is
    normally byte-identical to its predecessor's (same port, same
    secret), so URL content cannot be trusted to distinguish owners.
    ``_write_pid_url`` writes pid before url, so
    "pid is mine" implies the successor hasn't started writing; holding
    ``_serving_files_lock`` across check + unlink closes the remaining
    interleave window.
    """
    with _serving_files_lock():
        try:
            owns = _pid_file().read_text().strip() == str(os.getpid())
        except OSError:
            return
        if not owns:
            return
        for path in (_url_file(), _pid_file()):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()


if __name__ == "__main__":  # pragma: no cover — exercised end-to-end, not unit
    sys.exit(run_main())
