"""Persistent localhost settings UI for stdio-mode installations.

Stdio MCP servers (Claude Desktop, Claude Code, default Docker) run as
short-lived subprocesses spawned by the AI client. They idle-die and
get SIGTERM'd by known client bugs, which makes the in-process HTTP
settings page used by the ``ha-mcp-web`` / add-on entrypoints
unreachable when the user wants to open it.

This module addresses that by spawning a tiny standalone Starlette
HTTP server in a detached child process on stdio startup. The child
survives parent SIGTERM / idle-death, lives until the OS reboots (or
until the user disables it), and serves the same settings page the
HTTP modes serve — the route handlers are shared via
:func:`ha_mcp.settings_ui.build_settings_handlers` so there's no second
surface to maintain.

Security posture:
    - Bind 127.0.0.1 only (never the wildcard).
    - Random secret path generated per spawn (16 bytes urlsafe).
    - Random free port chosen at spawn time.
    - ``Host`` header validation: rejects requests whose host doesn't
      match the bound socket — blocks DNS rebinding attacks where a
      malicious website resolves an attacker-controlled domain to
      ``127.0.0.1`` to reach this listener from the user's browser.
    - ``Origin`` validation on mutating methods.
    - ``~/.ha-mcp/ui.{url,pid,log}`` written with 0600 / 0644 perms.

Disable mechanisms:
    - ``HA_MCP_DISABLE_SETTINGS_UI`` env var (truthy → skip spawn).
    - ``~/.ha-mcp/settings_ui_disabled`` sentinel file — created by the
      ``POST /shutdown`` endpoint, honored on next parent startup.

Lifecycle:
    - Parent stdio process calls :func:`maybe_spawn` shortly after
      argument validation, before entering the stdio event loop.
    - Child runs until killed (OS reboot, ``ha-mcp-settings stop``,
      or the ``POST /shutdown`` endpoint).
"""

from __future__ import annotations

import contextlib
import logging
import os
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
    if os.environ.get("HA_MCP_DISABLE_SETTINGS_UI", "").strip().lower() in _TRUTHY:
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

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
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


def _existing_sidecar_alive() -> bool:
    """Check whether a previously spawned sidecar is still running.

    "Alive" here means BOTH the recorded PID is live AND the URL file
    is present on disk. Checking the PID alone has two failure modes:

    * **PID reuse**: after a crash that doesn't clean up ``ui.pid``,
      the OS can reassign that PID to an unrelated process (any
      ``python.exe``, a system daemon, even ``chrome.exe``).
      ``_pid_alive`` returns True for any of those, so
      ``maybe_spawn()`` permanently skips spawning the real sidecar
      until the user manually deletes ``ui.pid``.

    * **Crashed-mid-startup**: child exits before writing
      ``ui.url`` but after writing ``ui.pid`` (e.g. uvicorn port-bind
      race, see ``_pick_free_port`` docstring). Same lockout.

    The URL file is the consumer contract — if it isn't present,
    no one can reach the sidecar, so by definition no sidecar is
    "serving". Self-heal by reporting False, which lets the caller
    spawn a fresh one and overwrite both stale files.
    """
    try:
        raw = _pid_file().read_text().strip()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    if not _pid_alive(pid):
        return False
    if not _url_file().exists():
        # PID is live but no URL on disk → the process is either a
        # reused-PID unrelated stranger, or a crashed-mid-startup
        # sidecar that never finished writing. Either way the
        # consumer can't reach it; treat as dead.
        logger.warning(
            "Sidecar pid %s is alive but %s is missing — "
            "treating as stale and respawning.",
            pid,
            _url_file(),
        )
        return False
    return True


def _spawn_lock_path() -> Path:
    return _sidecar_dir() / "spawn.lock"


@contextlib.contextmanager
def _spawn_lock() -> Iterator[bool]:
    """Yield True if this caller holds the spawn lock, False if another holds it.

    Serializes concurrent ``maybe_spawn()`` calls so two parent stdio
    processes starting in rapid succession can't both clear the
    ``_existing_sidecar_alive()`` check and ``Popen`` a child — the
    loser of which would race on ``bind()`` and crash into ``sidecar.log``.

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


def _pick_free_port() -> int:
    """Bind a transient socket to an ephemeral port and return it.

    The socket is closed before the sidecar opens its own listener.
    The OS may hand out the same port again to the sidecar; if another
    process snatches it in the gap, the sidecar startup will fail and
    log to ``sidecar.log`` — the parent moves on (settings UI is
    advisory, not required for MCP operation).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def maybe_spawn() -> None:
    """Spawn the sidecar if appropriate.

    Called once from stdio ``main()`` after argument validation. No-op
    when the sidecar is disabled (env var or sentinel), when another
    sidecar is already alive, when a concurrent parent already holds
    the spawn lock, or when subprocess spawn raises (best effort; the
    MCP server continues regardless).
    """
    if _is_disabled():
        logger.info(
            "Settings UI sidecar disabled (env var or %s sentinel); skipping spawn.",
            _disabled_sentinel().name,
        )
        return

    # Serialize concurrent spawn attempts. Two parent stdio processes
    # starting in rapid succession (e.g. user launching Claude Desktop
    # then Claude Code back-to-back) could both clear the alive-check
    # and Popen — the loser's child would race on bind() and die into
    # sidecar.log. The lock ensures only one parent runs the
    # alive-check + Popen window at a time.
    with _spawn_lock() as acquired:
        if not acquired:
            logger.info(
                "Another parent process is currently spawning the sidecar; skipping."
            )
            return

        # Re-check alive *inside* the lock — a concurrent parent that
        # held the lock just before us may have already spawned a
        # sidecar that has now written its pid file.
        if _existing_sidecar_alive():
            url = read_sidecar_url()
            if url:
                print(f"ha-mcp settings UI already running at: {url}", file=sys.stderr)
            logger.info("Settings UI sidecar already running; skipping spawn.")
            return

        _do_spawn()


def _do_spawn() -> None:
    """Inner spawn — assumes the spawn lock is held and alive-check failed.

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
        startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
        popen_kwargs["startupinfo"] = startupinfo
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
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


def _write_pid_url(url: str) -> None:
    """Persist the sidecar URL and pid for parent / overview consumption.

    Writes pid BEFORE url so a partial failure can't leave a URL file
    pointing at a dead port without the matching pid file. If the URL
    write fails after the pid file lands, both are removed — better to
    have neither than a URL+missing-pid pair that future
    ``maybe_spawn()`` calls would misread as "no sidecar".
    """
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
    from starlette.responses import JSONResponse, PlainTextResponse
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
        # Per-tool approval policy endpoints (#966). Pending/approve/deny
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
    ]

    # /shutdown — POST endpoint that drops the disable sentinel and
    # signals uvicorn to exit. The handler is wired in run_main() after
    # the server is constructed because we need a handle to the server
    # object — the ``shutdown_state`` dict is the indirection layer.
    shutdown_lock = threading.Lock()
    shutdown_state: dict[str, Callable[[], None] | None] = {"stop": None}

    async def _shutdown_endpoint(_request: Request) -> JSONResponse:
        # Drop sentinel BEFORE signalling exit so a fast restart cycle
        # doesn't race past the check in maybe_spawn(). If the sentinel
        # write fails, surface the failure to the caller AND keep the
        # sidecar running — silently exiting without the sentinel would
        # leave the user thinking they'd disabled the sidecar while it
        # quietly respawns on the next stdio start.
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
                # spawn the sidecar, and surface the failure.
                logger.exception("uvicorn stop() raised — rolling back sentinel")
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
        return JSONResponse(
            {
                "success": True,
                "message": (
                    "Settings UI sidecar shutting down. "
                    f"Delete {_disabled_sentinel()} to re-enable on next ha-mcp start."
                ),
            }
        )

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

    Picks a port, generates a secret path, writes pid+url files, and
    runs uvicorn until killed. Returns the exit code.
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

    port = _pick_free_port()
    secret_token = secrets.token_urlsafe(16)
    secret_path = f"/private_{secret_token}"
    url = f"http://127.0.0.1:{port}{secret_path}/settings"

    app = _build_app(host="127.0.0.1", port=port, secret_path=secret_path)

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level=log_level.lower(),
        access_log=False,
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

    _write_pid_url(url)

    logger.info("Settings UI sidecar listening at %s", url)
    print(f"ha-mcp settings UI ready at: {url}", file=sys.stderr)

    try:
        server.run()
    finally:
        # Best-effort cleanup of state files on graceful exit.
        for path in (_url_file(), _pid_file()):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.unlink()

    return 0


if __name__ == "__main__":  # pragma: no cover — exercised end-to-end, not unit
    sys.exit(run_main())
