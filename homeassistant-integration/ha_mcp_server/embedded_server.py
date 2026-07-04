"""Run the ha-mcp FastMCP server in-process inside Home Assistant (issue #1527).

The :class:`EmbeddedServerManager` owns the full lifecycle of the in-process
ha-mcp server:

* ensures the ``ha-mcp`` package is importable (runtime pip install via
  Home Assistant's requirements manager, honoring an options-flow pip-spec
  override for pre-release testing, and forcing a real reinstall when that spec
  changes),
* provisions a long-lived Home Assistant admin token the server uses to reach HA
  core over loopback (REST + WebSocket),
* runs the server on a dedicated thread with its own asyncio loop — uvicorn
  skips signal capture off the main thread and a heavy tool can never stall HA's
  event loop — and
* tears the thread down cleanly, and revokes the provisioned credentials when
  the entry is removed.

Everything the server needs from ha-mcp is imported **inside the worker thread**,
after the required non-secret environment variables are staged, so importing this
module never pulls in ``ha_mcp`` (which may not be installed yet) and never runs
before the connection is in place. The loopback URL and the admin token are handed
to ha-mcp **in memory** via ``ha_mcp.config.set_embedded_connection`` — never
through ``os.environ`` — so the admin token can never be read from the shared HA
process environment. ``ha_mcp`` module-level imports are therefore forbidden here.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import os
import subprocess
import sys
import threading
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.requirements import (
    RequirementsNotFound,
    async_process_requirements,
    pip_kwargs,
)
from homeassistant.util.package import install_package

from .const import (
    CHANNEL_DEV,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_CHANNEL,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DEV_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    SERVER_CONFIG_SUBDIR,
    SERVER_TOKEN_CLIENT_NAME,
    SERVER_USER_NAME,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Access-token longevity for the provisioned long-lived token. HA caps nothing
# here; ten years is effectively "for the life of the install" and is refreshed
# from the same refresh token on every start regardless.
_ACCESS_TOKEN_TTL = timedelta(days=3650)

# Readiness probe: how long to wait for the server thread to accept a loopback
# TCP connection before declaring the start failed.
_READY_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.5

# How long to wait for the worker thread to exit on stop before giving up and
# leaking it rather than blocking HA shutdown.
_STOP_JOIN_TIMEOUT_SECONDS = 10.0

# Per-download HTTP timeout for a forced reinstall. The first install pulls the
# whole fastmcp tree, well beyond HA's 60s requirements default.
_PIP_INSTALL_TIMEOUT_SECONDS = 300

# Uninstall just removes files/metadata, so it is quick; cap it so a wedged
# subprocess can never tie up an executor thread indefinitely.
_PIP_UNINSTALL_TIMEOUT_SECONDS = 120


class EmbeddedServerError(Exception):
    """Raised when the in-process ha-mcp server could not be installed or started.

    ``kind`` classifies the failure so the caller can file the matching repair
    issue: ``"package"`` for a pip install / import failure, ``"start"`` for
    everything else (token provisioning, thread crash, readiness timeout).
    """

    def __init__(self, message: str, *, kind: str = "start") -> None:
        """Store the message and the failure ``kind`` (``package`` / ``start``)."""
        super().__init__(message)
        self.kind = kind


class EmbeddedServerManager:
    """Manage the lifecycle of the in-process ha-mcp server for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind the manager to its Home Assistant instance and config entry."""
        self._hass = hass
        self._entry = entry

        options = entry.options
        self._port: int = int(options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
        self._bind_host: str = str(options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
        self._server_url: str = str(
            options.get(OPT_SERVER_URL) or DEFAULT_LOOPBACK_URL
        ).rstrip("/")
        self._channel: str = str(options.get(OPT_CHANNEL) or DEFAULT_CHANNEL)
        # An explicit pip-spec override (the pre-release test channel) wins over
        # the channel selector. DEFAULT_PIP_SPEC in the field means "no override,
        # use the channel" — the value moves with each release, so it must never
        # be treated as an intentional pin (the options flow also normalizes it
        # away on save; this guard keeps legacy/direct entries correct too).
        raw_pip_spec = str(options.get(OPT_PIP_SPEC) or "").strip()
        self._pip_spec_override: str = (
            raw_pip_spec if raw_pip_spec and raw_pip_spec != DEFAULT_PIP_SPEC else ""
        )
        self._pip_spec: str = self._resolve_pip_spec()
        self._secret_path: str = str(entry.data.get(DATA_SECRET_PATH, ""))
        self._config_dir: str = hass.config.path(SERVER_CONFIG_SUBDIR)

        # Worker-thread state. ``_loop`` and ``_stop_event`` are created in the
        # thread before its loop runs, so a stop request can always reach them.
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_exc: BaseException | None = None

    @property
    def port(self) -> int:
        """TCP port the server listens on."""
        return self._port

    @property
    def is_running(self) -> bool:
        """Return True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # -- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Install the package, provision a token, and start the server thread.

        Raises :class:`EmbeddedServerError` on any failure. The caller is
        responsible for surfacing a repair issue — a failed start must never take
        the rest of Home Assistant down with it.
        """
        if not self._secret_path:
            raise EmbeddedServerError(
                "Server secret path missing from the config entry; "
                "reload the integration to regenerate it."
            )

        await self._async_ensure_package()
        access_token = await self._async_provision_token()
        await self._hass.async_add_executor_job(self._prepare_config_dir)

        self._thread_exc = None
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(access_token,),
            name="ha-mcp-server",
            daemon=True,
        )
        self._thread.start()

        await self._async_wait_until_ready()

    async def async_stop(self) -> None:
        """Signal the worker thread to shut down and join it (bounded).

        Never blocks Home Assistant shutdown indefinitely: if the thread does not
        exit within the timeout it is logged and left to die with the process.
        Does NOT revoke the provisioned token — that is reserved for
        :meth:`async_revoke_credentials` (entry removal) so a reload keeps
        working.
        """
        thread = self._thread
        if thread is None:
            return

        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop_event.set)

        await self._hass.async_add_executor_job(thread.join, _STOP_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            _LOGGER.warning(
                "Home Assistant MCP Server thread did not stop within %.0fs; "
                "leaving it to terminate with the process.",
                _STOP_JOIN_TIMEOUT_SECONDS,
            )
        self._thread = None
        self._loop = None
        self._stop_event = None

    async def async_revoke_credentials(self) -> None:
        """Revoke the provisioned refresh token and remove the server's user.

        Called when the config entry is removed. Best-effort and idempotent:
        missing ids / already-deleted objects are treated as success.
        """
        rt_id = self._entry.data.get(DATA_REFRESH_TOKEN_ID)
        user_id = self._entry.data.get(DATA_SERVER_USER_ID)

        if rt_id:
            refresh_token = self._hass.auth.async_get_refresh_token(rt_id)
            if refresh_token is not None:
                self._hass.auth.async_remove_refresh_token(refresh_token)

        if user_id:
            user = await self._hass.auth.async_get_user(user_id)
            if user is not None:
                await self._hass.auth.async_remove_user(user)

        remaining = {
            k: v
            for k, v in self._entry.data.items()
            if k not in (DATA_SERVER_USER_ID, DATA_REFRESH_TOKEN_ID, DATA_ACCESS_TOKEN)
        }
        if remaining != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=remaining)

    # -- package install ---------------------------------------------------

    def _resolve_pip_spec(self) -> str:
        """Return the effective pip requirement for the configured channel.

        An explicit override wins (any pip requirement string — a version pin, a
        GitHub tarball URL — the pre-release test channel). Otherwise the channel
        picks the distribution: ``dev`` → the unpinned ``ha-mcp-dev`` (latest dev
        build), ``stable`` → the pinned ``ha-mcp==<PINNED>``.
        """
        if self._pip_spec_override:
            return self._pip_spec_override
        if self._channel == CHANNEL_DEV:
            return DEV_PIP_SPEC
        return DEFAULT_PIP_SPEC

    def _conflicting_dist_name(self) -> str | None:
        """Return the other channel's distribution name, or None to skip.

        ``dev`` installs ``ha-mcp-dev`` (conflicts with ``ha-mcp``) and ``stable``
        installs ``ha-mcp`` (conflicts with ``ha-mcp-dev``). Returns None for an
        explicit override, whose distribution name is unknown so nothing is
        removed.
        """
        if self._pip_spec_override:
            return None
        return DIST_NAME_STABLE if self._channel == CHANNEL_DEV else DIST_NAME_DEV

    async def _async_ensure_package(self) -> None:
        """Ensure ``ha-mcp`` is importable, installing the pip spec if needed.

        Fast path: when the configured pip spec matches the one last installed
        and the package imports, delegate the "already satisfied?" decision to
        Home Assistant's requirements manager. Otherwise (spec changed — the
        pre-release test channel — or the package is missing) force a real
        reinstall that bypasses the requirements manager's is-installed shortcut,
        so a changed spec actually takes effect.

        The ``dev`` channel ALWAYS takes the force-install path so every entry
        reload / HA restart picks up the newest ``ha-mcp-dev`` build. This runs in
        a background task, so it never blocks HA startup, and uv no-ops quickly
        when the newest build is already installed.

        On a channel switch the other channel's distribution is uninstalled first
        (:meth:`_async_remove_conflicting_dist`): ``ha-mcp`` and ``ha-mcp-dev``
        share the ``ha_mcp`` import package, so leaving both installed would make a
        pinned reinstall a no-op (breaking a dev→stable downgrade) and the reported
        version ambiguous.

        Never imports ``ha_mcp`` in this (main) process — that happens only inside
        the worker thread.
        """
        stored_spec = self._entry.data.get(DATA_LAST_PIP_SPEC)
        installed_version = await self._hass.async_add_executor_job(
            _installed_ha_mcp_version
        )

        fast_path_ok = (
            self._channel != CHANNEL_DEV
            and stored_spec == self._pip_spec
            and installed_version is not None
        )
        if fast_path_ok:
            await self._async_process_requirements_fast()
        else:
            await self._async_remove_conflicting_dist()
            await self._async_force_install()

        version = await self._hass.async_add_executor_job(_installed_ha_mcp_version)
        if version is None:
            raise EmbeddedServerError(
                f"Installed the server requirement ({self._pip_spec!r}) but the "
                "'ha-mcp' package is still not importable.",
                kind="package",
            )
        _LOGGER.info("Home Assistant MCP Server package ready (version %s)", version)
        if stored_spec != self._pip_spec:
            self._store_installed_spec()

    async def _async_process_requirements_fast(self) -> None:
        """Fast path: let HA's requirements manager satisfy the pinned spec."""
        try:
            await async_process_requirements(
                self._hass,
                f"{DOMAIN} server",
                [self._pip_spec],
                is_built_in=False,
            )
        except RequirementsNotFound as err:
            raise EmbeddedServerError(
                f"Could not install the server ({self._pip_spec!r}): {err}",
                kind="package",
            ) from err

    async def _async_force_install(self) -> None:
        """Force a real (re)install of the pip spec, bypassing the is-installed
        cache.

        Mirrors how ``homeassistant.requirements`` builds its pip invocation
        (HA's own constraints file + ``config/deps`` target where applicable) so
        the resolver honors Home Assistant's constraints, then installs with
        ``upgrade=True`` and a generous per-download timeout.
        """
        kwargs = pip_kwargs(self._hass.config.config_dir)
        kwargs["timeout"] = max(
            int(kwargs.get("timeout") or 0), _PIP_INSTALL_TIMEOUT_SECONDS
        )
        installed = await self._hass.async_add_executor_job(
            partial(install_package, self._pip_spec, upgrade=True, **kwargs)
        )
        if not installed:
            raise EmbeddedServerError(
                f"Could not install the server ({self._pip_spec!r}); see the "
                "Home Assistant log for the pip output.",
                kind="package",
            )

    async def _async_remove_conflicting_dist(self) -> None:
        """Uninstall the other release channel's distribution before installing.

        ``ha-mcp`` (stable) and ``ha-mcp-dev`` (dev) ship the *same* ``ha_mcp``
        import package, so installing one over the other overwrites the shared
        files while leaving both distributions' metadata behind. That stale
        metadata makes a later pinned reinstall a no-op (``ha-mcp==X`` looks
        already-satisfied, so a dev→stable downgrade would leave dev files on
        disk) and makes the reported version ambiguous. Removing the other
        channel's distribution first keeps exactly one installed.

        Best-effort: a failed uninstall is logged, not raised — the forced
        (re)install that follows still writes the correct channel's files, and the
        next reload retries the cleanup. Skipped for an explicit override, whose
        distribution name is unknown.
        """
        other = self._conflicting_dist_name()
        if other is None:
            return
        if not await self._hass.async_add_executor_job(_dist_installed, other):
            return
        _LOGGER.info(
            "Removing the other release channel's package %r before installing %r",
            other,
            self._pip_spec,
        )
        await self._hass.async_add_executor_job(_uninstall_distribution, other)

    def _store_installed_spec(self) -> None:
        """Persist the pip spec just installed so a restart skips the reinstall."""
        new_data = {**self._entry.data, DATA_LAST_PIP_SPEC: self._pip_spec}
        if new_data != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    # -- token provisioning ------------------------------------------------

    async def _async_provision_token(self) -> str:
        """Return an admin access token for the server, provisioning if needed.

        Reuses the previously-created local admin user and long-lived refresh
        token across restarts (ids persisted in ``entry.data``); a fresh access
        token is minted from the refresh token on every start. Falls back to
        creating a new user / refresh token when the stored ones are gone.
        """
        user_id = self._entry.data.get(DATA_SERVER_USER_ID)
        rt_id = self._entry.data.get(DATA_REFRESH_TOKEN_ID)

        user = await self._hass.auth.async_get_user(user_id) if user_id else None
        if user is None:
            user = await self._hass.auth.async_create_user(
                SERVER_USER_NAME,
                group_ids=[GROUP_ID_ADMIN],
                local_only=True,
            )
            rt_id = None

        refresh_token = (
            self._hass.auth.async_get_refresh_token(rt_id) if rt_id else None
        )
        if refresh_token is not None and refresh_token.user.id != user.id:
            refresh_token = None

        if refresh_token is None:
            # A long-lived token's client_name must be unique per user, so clear
            # any stale one left behind by a partial previous provision.
            for token in list(user.refresh_tokens.values()):
                if (
                    token.client_name == SERVER_TOKEN_CLIENT_NAME
                    and token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
                ):
                    self._hass.auth.async_remove_refresh_token(token)
            refresh_token = await self._hass.auth.async_create_refresh_token(
                user,
                client_name=SERVER_TOKEN_CLIENT_NAME,
                token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                access_token_expiration=_ACCESS_TOKEN_TTL,
            )

        access_token = self._hass.auth.async_create_access_token(refresh_token)

        new_data = {
            **self._entry.data,
            DATA_SERVER_USER_ID: user.id,
            DATA_REFRESH_TOKEN_ID: refresh_token.id,
            DATA_ACCESS_TOKEN: access_token,
        }
        if new_data != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)
        return access_token

    def _prepare_config_dir(self) -> None:
        """Create the server's persistent data directory (blocking)."""
        os.makedirs(self._config_dir, exist_ok=True)

    # -- worker thread -----------------------------------------------------

    def _thread_main(self, access_token: str) -> None:
        """Thread entry point: stage non-secret env, then run the server.

        Only the non-secret ``HA_MCP_CONFIG_DIR`` / ``HA_MCP_EMBEDDED`` variables
        are staged here (both read lazily throughout ha_mcp), and they MUST be set
        before the first ``ha_mcp`` import so data-dir resolution and embedded-mode
        detection see them. The loopback URL and the admin token are handed to
        ha_mcp in memory inside :meth:`_serve` — never via ``os.environ``.
        """
        os.environ["HA_MCP_CONFIG_DIR"] = self._config_dir
        os.environ["HA_MCP_EMBEDDED"] = "1"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        try:
            loop.run_until_complete(self._serve(access_token))
        except Exception as err:
            self._thread_exc = err
            _LOGGER.exception("Home Assistant MCP Server thread crashed")
        finally:
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _serve(self, access_token: str) -> None:
        """Build the ha-mcp server and run it until a stop is signaled.

        Mirrors the CLI HTTP runner in ``ha_mcp.__main__`` without importing it
        (that module runs process-global side effects — truststore SSL patching,
        signal handlers, ``asyncio.run`` — that must never happen in-process).
        """
        # Hand ha-mcp the loopback URL + provisioned admin token in memory, before
        # the server (and its settings singleton) is built. Keeping the token out
        # of os.environ is the whole point of the in-process channel.
        from ha_mcp.config import set_embedded_connection

        set_embedded_connection(self._server_url, access_token)

        # Imported here, in the worker thread, after the connection is registered.
        from ha_mcp.server import HomeAssistantSmartMCPServer
        from ha_mcp.settings_ui import register_settings_routes

        server = HomeAssistantSmartMCPServer()

        # Startup observability (no secrets): confirm the in-memory connection
        # channel actually reached the settings singleton — a sentinel here means
        # the server cannot talk to HA core and every tool call will fail.
        from ha_mcp.config import OAUTH_MODE_TOKEN, OAUTH_MODE_URL, get_global_settings

        resolved = get_global_settings()
        _LOGGER.info(
            "Embedded connection resolved: url=%s, token=%s (requested url=%s)",
            resolved.homeassistant_url,
            "provisioned"
            if resolved.homeassistant_token not in ("", OAUTH_MODE_TOKEN)
            else "SENTINEL-MISSING",
            self._server_url,
        )
        if resolved.homeassistant_url in ("", OAUTH_MODE_URL):
            _LOGGER.error(
                "Embedded connection did not apply (sentinel URL) — tool calls "
                "will fail. This indicates the in-process settings channel broke."
            )

        # Parity with the CLI HTTP runner: serve the web settings UI under the
        # same secret path as the MCP endpoint.
        register_settings_routes(server.mcp, server, secret_path=self._secret_path)

        # Own the uvicorn server instead of calling mcp.run_async(): cancelling
        # run_async's task does NOT release the listening socket in-process
        # (live-found: the next bring-up failed with EADDRINUSE and uvicorn's
        # lifespan generator raised "athrow(): asynchronous generator is
        # already running"). The CLI never sees this because its process exits.
        # This mirrors fastmcp 3.4.2's run_http_async internals (http_app +
        # uvicorn.Config defaults + _lifespan_manager), pinned by ha-mcp.
        import uvicorn

        app = server.mcp.http_app(path=self._secret_path, stateless_http=True)
        config = uvicorn.Config(
            app,
            host=self._bind_host,
            port=self._port,
            timeout_graceful_shutdown=2,
            lifespan="on",
            ws="websockets-sansio",
            # Leave Home Assistant's logging untouched — do not let uvicorn
            # reconfigure the root logger from this thread.
            log_config=None,
        )
        uv_server = uvicorn.Server(config)

        assert self._stop_event is not None
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        async with server.mcp._lifespan_manager():
            serve_task = asyncio.ensure_future(uv_server.serve())
            done, _pending = await asyncio.wait(
                {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                # Graceful shutdown through uvicorn's own path: waits out
                # in-flight requests (2s cap), runs lifespan shutdown, and
                # deterministically releases the socket for the next bring-up.
                uv_server.should_exit = True
                await serve_task
            else:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                # Surface a server that exited on its own (bind failure, etc.).
                serve_task.result()

    async def _async_wait_until_ready(self) -> None:
        """Poll a loopback TCP connect until the server accepts, or fail.

        On failure (timeout or an early thread crash) stops the thread and raises
        :class:`EmbeddedServerError` so the caller leaves the webhook
        unregistered and files a repair issue.
        """
        deadline = self._hass.loop.time() + _READY_TIMEOUT_SECONDS
        while self._hass.loop.time() < deadline:
            if self._thread_exc is not None:
                raise EmbeddedServerError(
                    f"Home Assistant MCP Server failed to start: {self._thread_exc}"
                ) from self._thread_exc
            if self._thread is not None and not self._thread.is_alive():
                raise EmbeddedServerError(
                    "Home Assistant MCP Server thread exited during startup."
                )
            if await self._async_probe_port():
                _LOGGER.info(
                    "Home Assistant MCP Server is listening on %s:%d",
                    self._bind_host,
                    self._port,
                )
                return
            await asyncio.sleep(_READY_POLL_INTERVAL_SECONDS)

        # Timed out — tear the thread down so we never leave a half-started
        # server behind an unregistered webhook.
        await self.async_stop()
        raise EmbeddedServerError(
            f"Home Assistant MCP Server did not become reachable on port "
            f"{self._port} within {_READY_TIMEOUT_SECONDS:.0f}s."
        )

    async def _async_probe_port(self) -> bool:
        """Return True if a loopback TCP connection to the server port succeeds.

        Probes 127.0.0.1 regardless of bind host — a 0.0.0.0 bind still accepts
        on loopback, and the forwarding webhook only ever talks to loopback.
        """
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._port),
                timeout=_READY_POLL_INTERVAL_SECONDS,
            )
        except (TimeoutError, OSError):
            return False
        writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        return True


def _installed_ha_mcp_version() -> str | None:
    """Return the installed ha-mcp distribution version, or None (blocking).

    Invalidates the import caches first so a just-completed pip install is seen.
    Checks both the stable (``ha-mcp``) and dev (``ha-mcp-dev``) distribution
    names, mirroring ``ha_mcp._version.get_version``.
    """
    importlib.invalidate_caches()
    for dist_name in (DIST_NAME_STABLE, DIST_NAME_DEV):
        with suppress(importlib.metadata.PackageNotFoundError):
            return importlib.metadata.version(dist_name)
    return None


def _dist_installed(dist_name: str) -> bool:
    """Return True if the named distribution has installed metadata (blocking).

    Invalidates the import caches first so a just-completed (un)install is seen.
    """
    importlib.invalidate_caches()
    try:
        importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _uninstall_distribution(dist_name: str) -> None:
    """Uninstall a distribution by name (blocking), best-effort.

    Mirrors ``homeassistant.util.package.install_package``'s invocation style —
    ``<python> -m uv pip ...`` with an explicit ``--python`` and no shell — so it
    targets the same interpreter environment Home Assistant installed the package
    into. A failure is logged but not raised; the caller treats the cleanup as
    best-effort.
    """
    args = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "uninstall",
        "--python",
        sys.executable,
        dist_name,
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_PIP_UNINSTALL_TIMEOUT_SECONDS,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.warning("Could not uninstall %r: %s", dist_name, err)
        return
    if result.returncode != 0:
        _LOGGER.warning(
            "Uninstall of %r exited %d: %s",
            dist_name,
            result.returncode,
            (result.stderr or "").strip(),
        )
