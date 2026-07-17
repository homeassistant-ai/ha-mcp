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
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.requirements import (
    RequirementsNotFound,
    async_process_requirements,
    pip_kwargs,
)
from homeassistant.util.package import install_package
from packaging.version import InvalidVersion, Version

from .const import (
    CHANNEL_DEV,
    DATA_ACCESS_TOKEN,
    DATA_LAST_PIP_SPEC,
    DATA_PENDING_INSTALL_VERSION,
    DATA_REFRESH_TOKEN_ID,
    DATA_SECRET_PATH,
    DATA_SERVER_USER_ID,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_BIND_HOST,
    DEFAULT_CHANNEL,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
    OPT_AUTO_UPDATE,
    OPT_BIND_HOST,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    SERVER_CONFIG_SUBDIR,
    SERVER_TOKEN_CLIENT_NAME,
    SERVER_USER_NAME,
    dist_for_channel,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Access-token longevity for the provisioned long-lived token. HA caps nothing
# here; ten years is effectively "for the life of the install" and is refreshed
# from the same refresh token on every start regardless.
_ACCESS_TOKEN_TTL = timedelta(days=3650)

# Readiness probe: fail the bring-up only when there is no observable startup
# progress (no new modules landing in sys.modules, no phase advance) for this
# long. The previous flat 90s deadline assumed real hardware imports the
# server tree in seconds — issue #1904 (HA Green) showed a cold import alone
# can take minutes there, and killing the still-importing worker at the
# deadline is what created the orphaned-thread/port-collision cascade: the
# worker cannot be joined mid-import, lingered as a zombie, and later bound
# the port out from under the retry. The module count is process-wide (see
# _progress_signature), so a wedged worker trips the stall budget on a quiet
# instance and the absolute cap below at the latest.
_READY_STALL_TIMEOUT_SECONDS = 90.0
# Absolute ceiling on one bring-up regardless of apparent progress, matching
# the HAOS e2e lane's own 600s readiness deadline.
_READY_TOTAL_CAP_SECONDS = 600.0
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

# The in-process connection API was added with the embedded server in 7.10.0.
# Older distributions can be left behind by an unsupported Core version's
# constraints and must never enter the worker thread.
MIN_EMBEDDED_SERVER_VERSION = "7.10.0"


def _derive_loopback_url(hass: HomeAssistant) -> tuple[str, bool | None]:
    """Resolve the loopback base URL for HA core from the http integration.

    Returns ``(url, verify_ssl)`` where ``verify_ssl`` is ``False`` when the
    URL is ``https`` (HA's certificate is issued for its hostname, never for
    127.0.0.1, so verification on the loopback hop can only fail) and ``None``
    when no override of the server's default is needed.

    The hardcoded ``http://127.0.0.1:8123`` default this replaces broke every
    instance with ``http.ssl_certificate`` configured (issue #1890): port 8123
    speaks TLS there, so the server's plaintext REST/WS calls died with
    "Server disconnected without sending a response" / "did not receive a
    valid HTTP response" on every tool call — while the MCP handshake and
    tools/list (no HA round-trip) kept working. A custom ``server_port``
    similarly broke the hardcoded port. Both live in ``hass.config.api``,
    set by the ``http`` integration this component depends on; the constant
    remains the fallback if it is ever absent.
    """
    api = getattr(hass.config, "api", None)
    if api is None:
        # Leave a trail: if this ever fires on a real instance, the resulting
        # failure looks exactly like issue #1890 (TLS loopback broken, MCP
        # handshake fine) and took a live reproduction to diagnose last time.
        _LOGGER.debug(
            "hass.config.api unavailable; using hardcoded loopback default %s",
            DEFAULT_LOOPBACK_URL,
        )
        return DEFAULT_LOOPBACK_URL, None
    # Strict type checks (not coercion / truthiness): a malformed api object
    # must resolve to the plaintext default on port 8123, never to a surprise
    # port or https flip. bool is excluded because it is an int subclass.
    port_raw = getattr(api, "port", None)
    port = (
        port_raw
        if isinstance(port_raw, int)
        and not isinstance(port_raw, bool)
        and 0 < port_raw <= 65535
        else 8123
    )
    if getattr(api, "use_ssl", False) is True:
        return f"https://127.0.0.1:{port}", False
    return f"http://127.0.0.1:{port}", None


class EmbeddedServerError(Exception):
    """Raised when the in-process ha-mcp server could not be installed or started.

    ``kind`` classifies the failure so the caller can file the matching repair
    issue: ``"package"`` for a pip install / import failure, ``"start"`` for
    everything else (token provisioning, thread crash, readiness timeout).
    """

    def __init__(
        self, message: str, *, kind: Literal["package", "start"] = "start"
    ) -> None:
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
        # An explicit server_url override wins verbatim (the operator manages
        # scheme/verification themselves via the settings UI). A stored value
        # equal to DEFAULT_LOOPBACK_URL is treated as no-override: the options
        # form used to pre-fill that constant as suggested_value, so existing
        # entries carry it without the user ever having chosen it.
        _url_override = str(options.get(OPT_SERVER_URL) or "").rstrip("/")
        self._loopback_verify_ssl: bool | None = None
        if _url_override and _url_override != DEFAULT_LOOPBACK_URL:
            self._server_url: str = _url_override
        else:
            self._server_url, self._loopback_verify_ssl = _derive_loopback_url(hass)
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
        # Auto-update toggle (default on). Off pins a non-override channel to
        # the currently-installed version; the periodic PyPI check keeps
        # running either way (it feeds the update entity — issue #1760), only
        # the automatic reload is gated on this. Read before
        # _resolve_pip_spec, which consults it.
        self._auto_update: bool = bool(
            options.get(OPT_AUTO_UPDATE, DEFAULT_AUTO_UPDATE)
        )
        # Initial spec without the installed-version read (that would block the
        # event loop). For an auto-update-off channel this is the bare dist here;
        # _async_ensure_package re-resolves it with the executor-read version
        # before installing.
        self._pip_spec: str = self._resolve_pip_spec()
        self._secret_path: str = str(entry.data.get(DATA_SECRET_PATH, ""))
        self._config_dir: str = hass.config.path(SERVER_CONFIG_SUBDIR)

        # Worker-thread state. ``_loop`` and ``_stop_event`` are created in the
        # thread before its loop runs, so a stop request can always reach them.
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_exc: BaseException | None = None
        # A worker that refused to die within the stop-join timeout (e.g.
        # wedged in a slow cold import). Tracked so the next start can skip
        # the module purge while it might still be importing.
        self._orphaned_thread: threading.Thread | None = None
        # Version reported by the CURRENT worker thread (stashed by _serve).
        # Compared against the installed distribution after start to detect a
        # stale-code worker (see _purge_ha_mcp_modules).
        self._running_version: str | None = None
        # Startup phase marker (plain attribute writes: init markers from the
        # main thread, _note_startup_phase transitions from the worker). Read
        # by the readiness poll for progress detection and error messages.
        self._startup_phase: str = "not started"

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
        try:
            home_assistant_version = Version(HA_VERSION)
        except InvalidVersion as err:
            raise EmbeddedServerError(
                f"The in-process server could not determine whether Home "
                f"Assistant {HA_VERSION} satisfies its minimum requirement of "
                f"{MIN_EMBEDDED_HOME_ASSISTANT_VERSION}. Install a standard Home "
                "Assistant release before reloading this integration.",
                kind="package",
            ) from err
        if home_assistant_version < Version(MIN_EMBEDDED_HOME_ASSISTANT_VERSION):
            raise EmbeddedServerError(
                f"The in-process server requires Home Assistant "
                f"{MIN_EMBEDDED_HOME_ASSISTANT_VERSION} or newer; this instance "
                f"is running {HA_VERSION}. Update Home Assistant before "
                "reloading this integration.",
                kind="package",
            )

        ready_version = await self._async_ensure_package()
        access_token = await self._async_provision_token()
        await self._hass.async_add_executor_job(self._prepare_config_dir)

        # Drop cached ha_mcp modules so the worker imports the code that is on
        # disk NOW. Without this, a reload after a pip install keeps serving
        # the OLD code forever: all workers are threads of the one HA core
        # process, and Python resolves ``import ha_mcp`` from sys.modules —
        # installs only took effect after a full HA core restart (issue
        # observed live: options saves reinstalled the package, the web UI
        # footer showed the new on-disk version, yet the serving worker kept
        # reporting the version it was first imported with).
        #
        # SKIPPED while an orphaned worker may still be importing: ripping
        # entries out of sys.modules under a live importer corrupts its
        # import in progress (seen on QEMU-slow HAOS, where a cold import
        # can outlive both the readiness timeout and the stop-join budget).
        # The post-start staleness check below surfaces the consequence
        # (old code possibly serving) instead.
        #
        # Also skipped when the cached modules already ARE the generation on
        # disk (issue #1904): purging on every attempt made each retry pay
        # the full cold import again, so slow hardware that missed the
        # readiness window once could never recover. Never skipped under a
        # pip-spec override — that is the one workflow where a reinstall can
        # change the code without changing the version string (re-pointed
        # tarball/pin), which a version-keyed skip would serve stale; channel
        # installs mint a distinct version per build.
        orphan = self._orphaned_thread
        if orphan is not None and not orphan.is_alive():
            self._orphaned_thread = orphan = None
        if orphan is not None:
            _LOGGER.warning(
                "Skipping the ha_mcp module purge: a previous worker thread "
                "is still shutting down. The new worker may serve the "
                "previously imported code until Home Assistant restarts."
            )
        elif (
            not self._pip_spec_override
            and ready_version is not None
            and _CACHED_IMPORT_VERSION is not None
            and ready_version == _CACHED_IMPORT_VERSION
        ):
            _LOGGER.debug(
                "Cached ha_mcp modules already match installed version %s; "
                "skipping the module purge (warm start)",
                ready_version,
            )
        else:
            _purge_ha_mcp_modules()

        self._thread_exc = None
        self._startup_phase = "waiting for the worker thread"
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(access_token,),
            name="ha-mcp-server",
            daemon=True,
        )
        self._thread.start()

        await self._async_wait_until_ready()

        # Belt-and-braces staleness check: the worker stashed the version
        # reported by the package it imported; if that disagrees with the
        # installed distribution the purge did not fully take (e.g. a stray
        # import of ha_mcp outside the worker re-cached old modules) and only an
        # HA core restart applies the update — say so instead of serving old
        # code silently.
        if self._running_version:
            installed = await self._hass.async_add_executor_job(
                _installed_ha_mcp_version, dist_for_channel(self._channel)
            )
            if installed and installed != self._running_version:
                _LOGGER.warning(
                    "HA-MCP in-process server is running version %s but "
                    "version %s is installed; restart Home Assistant to "
                    "finish applying the update.",
                    self._running_version,
                    installed,
                )

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
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # The worker's loop closed between the is_closed() check and
                # this call (thread already exiting) - join below handles it.
                pass

        await self._hass.async_add_executor_job(thread.join, _STOP_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            _LOGGER.warning(
                "HA-MCP in-process server thread did not stop within %.0fs; "
                "leaving it to terminate with the process.",
                _STOP_JOIN_TIMEOUT_SECONDS,
            )
            # Remember the zombie: the next start must not purge modules
            # while this thread may still be importing them. The worker
            # holds its own loop/stop-event as locals, so clearing the
            # published references below cannot crash it.
            self._orphaned_thread = thread
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._thread_exc = None
        self._running_version = None

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

    def _resolve_pip_spec(self, installed_version: str | None = None) -> str:
        """Return the effective pip requirement for the configured channel.

        An explicit override wins (any pip requirement string — a version pin, a
        GitHub tarball URL — the pre-release test channel). Otherwise the channel
        picks the distribution (``dev`` → ``ha-mcp-dev``, ``stable`` → ``ha-mcp``):

        * auto-update ON (default): the bare, unpinned distribution name, so the
          newest build of the channel resolves at install time.
        * auto-update OFF: the distribution pinned to ``installed_version``
          (``dist==X``), so reloads/restarts keep that exact version; falls back
          to the unpinned name when ``installed_version`` is None (nothing
          installed yet — first setup has no version to pin to, so it installs
          the newest once).

        ``installed_version`` is passed in (never read here) so this stays a pure,
        non-blocking function: the ``importlib.metadata`` read that discovers it
        happens on the executor in :meth:`_async_ensure_package`, off the loop.
        """
        if self._pip_spec_override:
            return self._pip_spec_override
        dist = dist_for_channel(self._channel)
        if not self._auto_update and installed_version is not None:
            return f"{dist}=={installed_version}"
        return dist

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

    async def _async_ensure_package(self) -> str | None:
        """Ensure ``ha-mcp`` is importable, installing the pip spec if needed.

        Returns the installed version that the worker is about to run, for the
        caller's warm-cache purge decision.

        With auto-update on (the default) both channels install their
        distribution UNPINNED, so every entry reload / HA restart must pick up
        the newest build. Such a spec ALWAYS takes the force-install path
        (``upgrade=True``, bypassing the requirements manager's is-installed
        shortcut) — that is what makes the channel auto-update. This runs in a
        background task, so it never blocks HA startup, and uv no-ops quickly
        when the newest build is already installed.

        Fast path: reserved for a STABLE spec — an explicit pip-spec override (a
        version pin or tarball URL) or a channel with auto-update turned OFF
        (which pins to the installed version, see :meth:`_resolve_pip_spec`).
        When that spec matches the one last installed and the package imports,
        delegate the "already satisfied?" decision to Home Assistant's
        requirements manager; a pinned spec does not move, so there is nothing to
        upgrade to. A CHANGED spec (a new override, a toggled auto-update, a
        channel switch) still falls through to the force-install path below so
        the change actually takes effect.

        On a channel switch the other channel's distribution is uninstalled first
        (:meth:`_async_remove_conflicting_dist`): ``ha-mcp`` and ``ha-mcp-dev``
        share the ``ha_mcp`` import package, so leaving both installed would make a
        pinned reinstall a no-op (breaking a dev→stable downgrade) and the reported
        version ambiguous.

        A one-shot pending-install marker (:data:`DATA_PENDING_INSTALL_VERSION`,
        set by the update entity's Install button — issue #1760) overrides both
        the unpinned-channel and auto-update-off pinning above for this single
        install, regardless of the ``auto_update`` option.

        Never imports ``ha_mcp`` in this (main) process — that happens only inside
        the worker thread.
        """
        stored_spec = self._entry.data.get(DATA_LAST_PIP_SPEC)
        installed_version = await self._hass.async_add_executor_job(
            _installed_ha_mcp_version
        )

        pending_version = str(
            self._entry.data.get(DATA_PENDING_INSTALL_VERSION) or ""
        ).strip()
        target_dist = dist_for_channel(self._channel)
        if not self._pip_spec_override and pending_version:
            # Pin to the requested version. Its own value differs from
            # stored_spec below (that is the whole point of the marker), which
            # already forces the force-install branch further down — no
            # separate fast-path handling needed here.
            #
            # Consumed HERE, before the install attempt: one-shot means one
            # ATTEMPT, not "until it succeeds". If it were cleared only on
            # success, a marker for a failing version would re-pin every later
            # reload — including the periodic auto-update ones — to that same
            # broken version, looping the failure forever while auto-update
            # looks on (review finding).
            self._pip_spec = f"{target_dist}=={pending_version}"
            self._clear_pending_install_marker()
        elif not self._pip_spec_override and not self._auto_update:
            # Re-pin an auto-update-off channel to its TARGET distribution's
            # installed version, read off-loop (the __init__ value was the bare
            # dist to avoid a blocking read on the event loop). Reading the
            # target dist specifically — not whichever dist happens to be
            # present — keeps a cross-channel switch correct: the previous
            # channel's dist is still installed at this point (removal happens
            # below), so a whichever-present read would pin the new dist to a
            # version that does not exist for it and fail the install. Nothing
            # of the target channel installed yet => None => stays unpinned and
            # installs the newest once.
            pin_version = await self._hass.async_add_executor_job(
                _installed_dist_version, target_dist
            )
            if pin_version is not None and not _is_compatible_embedded_version(
                pin_version
            ):
                _LOGGER.warning(
                    "Ignoring auto-update pin to legacy %s %s; the in-process "
                    "server requires %s or newer",
                    target_dist,
                    pin_version,
                    MIN_EMBEDDED_SERVER_VERSION,
                )
                self._pip_spec = target_dist
            else:
                self._pip_spec = self._resolve_pip_spec(pin_version)

        # A "stable" spec (an explicit override, or a channel pinned because
        # auto-update is off) is eligible for the fast path; an unpinned
        # auto-updating channel never is.
        spec_is_stable = bool(self._pip_spec_override) or not self._auto_update
        fast_path_ok = (
            spec_is_stable
            and stored_spec == self._pip_spec
            and installed_version is not None
            and _is_compatible_embedded_version(installed_version)
        )
        if fast_path_ok:
            await self._async_process_requirements_fast()
        else:
            await self._async_remove_conflicting_dist()
            await self._async_remove_legacy_target(target_dist, installed_version)
            await self._async_force_install()

        version: str | None
        if not self._pip_spec_override and self._channel == CHANNEL_DEV:
            version = await self._hass.async_add_executor_job(
                _installed_ha_mcp_version, target_dist
            )
        else:
            version = await self._hass.async_add_executor_job(_installed_ha_mcp_version)
        if version is None:
            raise EmbeddedServerError(
                f"Installed the server requirement ({self._pip_spec!r}) but the "
                "'ha-mcp' package is still not importable.",
                kind="package",
            )
        if not _is_compatible_embedded_version(version):
            raise EmbeddedServerError(
                f"The installer left installed ha-mcp {version}, but this "
                f"in-process component requires {MIN_EMBEDDED_SERVER_VERSION} "
                "or newer. Review resolver details logged under "
                "homeassistant.util.package, correct the package conflict, and "
                "reload this integration.",
                kind="package",
            )
        _LOGGER.info("HA-MCP in-process server package ready (version %s)", version)
        if stored_spec != self._pip_spec:
            self._store_installed_spec()
        return version

    async def _async_remove_legacy_target(
        self, target_dist: str, installed_version: str | None
    ) -> None:
        """Remove an incompatible target distribution before reinstalling it."""
        if installed_version is None or _is_compatible_embedded_version(
            installed_version
        ):
            return
        target_installed_version = await self._hass.async_add_executor_job(
            _installed_dist_version, target_dist
        )
        if target_installed_version is None or _is_compatible_embedded_version(
            target_installed_version
        ):
            return
        _LOGGER.warning(
            "Removing legacy %s %s before installing %r; the in-process "
            "server requires %s or newer",
            target_dist,
            target_installed_version,
            self._pip_spec,
            MIN_EMBEDDED_SERVER_VERSION,
        )
        await self._async_remove_distribution(target_dist)

    async def _async_process_requirements_fast(self) -> None:
        """Fast path: let HA's requirements manager satisfy the override spec."""
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
                f"Could not install the server ({self._pip_spec!r}). The "
                f"in-process server requires ha-mcp "
                f"{MIN_EMBEDDED_SERVER_VERSION} or newer and Home Assistant "
                f"{MIN_EMBEDDED_HOME_ASSISTANT_VERSION} or newer. Resolver "
                "details are logged under homeassistant.util.package.",
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
        await self._async_remove_distribution(other)

    async def _async_remove_distribution(self, dist_name: str) -> None:
        """Remove a distribution from the same target used for installation."""
        target = pip_kwargs(self._hass.config.config_dir).get("target")
        if target is None:
            await self._hass.async_add_executor_job(_uninstall_distribution, dist_name)
        else:
            await self._hass.async_add_executor_job(
                partial(_uninstall_distribution, dist_name, target=target)
            )

    def _store_installed_spec(self) -> None:
        """Persist the pip spec just installed so a restart skips the reinstall."""
        new_data = {**self._entry.data, DATA_LAST_PIP_SPEC: self._pip_spec}
        if new_data != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    def _clear_pending_install_marker(self) -> None:
        """Clear the update entity's one-shot pending-install marker.

        Called at CONSUME time in :meth:`_async_ensure_package`, before the
        install attempt runs: the marker buys exactly one attempt. Clearing
        only on success would let a marker for a failing version re-pin every
        later reload to that broken version (review finding).
        """
        if DATA_PENDING_INSTALL_VERSION not in self._entry.data:
            return
        new_data = dict(self._entry.data)
        new_data.pop(DATA_PENDING_INSTALL_VERSION, None)
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

        # hass is untyped here (homeassistant mocked in unit tier); pin str.
        access_token = str(self._hass.auth.async_create_access_token(refresh_token))

        # Persist only the ids needed to REUSE the credentials next start.
        # The access token itself is deliberately NOT stored: it is handed to
        # the worker in memory, nothing ever reads it back from entry.data,
        # and a fresh JWT is minted each start - persisting it would leave an
        # unused admin token in .storage AND rewrite the config entry on
        # every start (each mint differs). Review finding; the revoke path
        # still strips the legacy key from entries written by older builds.
        new_data = {
            **self._entry.data,
            DATA_SERVER_USER_ID: user.id,
            DATA_REFRESH_TOKEN_ID: refresh_token.id,
        }
        new_data.pop(DATA_ACCESS_TOKEN, None)
        if new_data != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)
        return access_token

    def _prepare_config_dir(self) -> None:
        """Create the server's persistent data directory (blocking)."""
        os.makedirs(self._config_dir, exist_ok=True)

    # -- worker thread -----------------------------------------------------

    def _note_startup_phase(self, phase: str) -> None:
        """Publish the worker's startup phase (a plain attribute write).

        Read by the readiness poll: a phase advance counts as progress, and the
        failure message names the phase the worker was last seen in.
        """
        self._startup_phase = phase
        _LOGGER.debug("HA-MCP in-process server startup: %s", phase)

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
        # Created here and handed to _serve as a LOCAL; publishing to self
        # is one-way, for async_stop's signaling only — see async_stop's
        # zombie-thread comment for why the worker must never re-read it.
        stop_event = asyncio.Event()
        self._loop = loop
        self._stop_event = stop_event
        try:
            loop.run_until_complete(self._serve(access_token, stop_event))
        except SystemExit as err:
            # uvicorn signals a startup failure (e.g. the port is already in
            # use) with SystemExit(STARTUP_FAILURE), which ``except Exception``
            # misses — live issue #1904 saw the real bind error surface only
            # in HA's generic task-exception log while the component reported
            # a bare readiness timeout. Unwrap the original error so the
            # repair issue names the actual cause; a bare SystemExit (no
            # chained exception) is reported by repr so an empty/zero exit
            # code still reads as what it is. The phase names where in
            # _serve the exit happened instead of hardcoding a bind failure.
            cause = err.__context__ or err.__cause__
            detail = str(cause) if cause is not None else repr(err)
            self._thread_exc = EmbeddedServerError(
                f"the server exited during startup ({self._startup_phase}): {detail}"
            )
            _LOGGER.error(
                "HA-MCP in-process server exited during startup (%s): %s",
                self._startup_phase,
                detail,
            )
        except Exception as err:
            self._thread_exc = err
            _LOGGER.exception("HA-MCP in-process server thread crashed")
        finally:
            # Teardown is best-effort but never SILENT (review finding): a
            # raise here must not mask the primary outcome, yet a recurring
            # cleanup failure (leaking executor threads across reloads) has
            # to be visible in the logs. Each call gets its own guard so one
            # failure cannot skip the other.
            for _label, _coro_factory in (
                ("asyncgen", loop.shutdown_asyncgens),
                ("executor", loop.shutdown_default_executor),
            ):
                try:
                    loop.run_until_complete(_coro_factory())
                except Exception:
                    _LOGGER.warning(
                        "Worker-loop %s shutdown failed during teardown",
                        _label,
                        exc_info=True,
                    )
            loop.close()

    async def _serve(self, access_token: str, stop_event: asyncio.Event) -> None:
        """Build the ha-mcp server and run it until a stop is signaled.

        Mirrors the CLI HTTP runner in ``ha_mcp.__main__`` without importing it
        (that module runs process-global side effects — truststore SSL patching,
        signal handlers, ``asyncio.run`` — that must never happen in-process).
        """
        # Hand ha-mcp the loopback URL + provisioned admin token in memory, before
        # the server (and its settings singleton) is built. Keeping the token out
        # of os.environ is the whole point of the in-process channel.
        self._note_startup_phase("importing the server package")
        import ha_mcp.config as _hamcp_config

        # Record which code generation this worker imported. Prefer the
        # configured channel when both distributions have metadata because
        # ha_mcp.__version__ itself checks stable first and stale stable
        # metadata can otherwise make a fresh dev worker look outdated.
        self._running_version = _running_ha_mcp_version(self._channel)
        # The cache in sys.modules now holds this generation — remembered
        # process-wide so the next start can skip the purge when the install
        # has not changed (issue #1904).
        global _CACHED_IMPORT_VERSION
        _CACHED_IMPORT_VERSION = self._running_version

        # Drop any settings singleton cached by a PREVIOUS start in this same
        # Python process: an entry reload must re-read the override files
        # (feature flags, advanced settings) exactly like an add-on restart
        # does. Fall back to the private seam on releases that predate the
        # public alias.
        _reset = getattr(
            _hamcp_config,
            "reset_global_settings",
            getattr(_hamcp_config, "_reset_global_settings", None),
        )
        if _reset is not None:
            _reset()
        else:
            _LOGGER.warning(
                "ha_mcp.config exposes no settings-reset seam; a reloaded "
                "entry may serve stale override values until HA restarts"
            )

        if self._loopback_verify_ssl is None:
            _hamcp_config.set_embedded_connection(self._server_url, access_token)
        else:
            try:
                _hamcp_config.set_embedded_connection(
                    self._server_url,
                    access_token,
                    verify_ssl=self._loopback_verify_ssl,
                )
            except TypeError:
                # Server predates the verify_ssl parameter (< the release
                # carrying issue #1890's fix). Register url+token the old way;
                # on an SSL-enabled instance the wss loopback will fail
                # certificate verification until the server package updates —
                # no worse than the plaintext failure it replaces.
                _LOGGER.warning(
                    "Installed ha-mcp server does not accept verify_ssl for "
                    "the embedded connection; loopback TLS verification stays "
                    "enabled until the server package updates"
                )
                _hamcp_config.set_embedded_connection(self._server_url, access_token)

        # Imported here, in the worker thread, after the connection is registered.
        from ha_mcp.server import HomeAssistantSmartMCPServer
        from ha_mcp.settings_ui import register_settings_routes

        self._note_startup_phase("building the server")
        server = HomeAssistantSmartMCPServer()

        # Startup observability (no secrets): confirm the in-memory connection
        # channel actually reached the settings singleton — a sentinel here means
        # the server cannot talk to HA core and every tool call will fail.
        OAUTH_MODE_TOKEN = _hamcp_config.OAUTH_MODE_TOKEN
        OAUTH_MODE_URL = _hamcp_config.OAUTH_MODE_URL
        get_global_settings = _hamcp_config.get_global_settings

        resolved = get_global_settings()
        _LOGGER.info(
            "Embedded connection resolved: url=%s, token=%s (requested url=%s)",
            resolved.homeassistant_url,
            "provisioned"
            if resolved.homeassistant_token not in ("", OAUTH_MODE_TOKEN)
            else "SENTINEL-MISSING",
            self._server_url,
        )
        if resolved.homeassistant_url in (
            "",
            OAUTH_MODE_URL,
        ) or resolved.homeassistant_token in ("", OAUTH_MODE_TOKEN):
            # Refuse to serve: a sentinel connection means every tool call
            # would fail while the bring-up still looked successful (webhook
            # registered, connect-URL notification shown). Raising propagates
            # via _thread_exc -> the readiness probe -> a repair issue, which
            # is the honest outcome (live-found signal-swallow). URL and
            # token are checked SYMMETRICALLY - today they can only fail
            # jointly, but the guard must catch a future regression in
            # either half of the in-memory channel.
            raise EmbeddedServerError(
                "The in-process settings channel did not apply - the server "
                "has no Home Assistant connection (sentinel URL or token). "
                "Refusing to start."
            )

        # Parity with the CLI HTTP runner: serve the web settings UI under the
        # same secret path as the MCP endpoint.
        self._note_startup_phase("registering web routes")
        register_settings_routes(server.mcp, server, secret_path=self._secret_path)

        # Parity with the CLI HTTP runner: answer a browser GET on the MCP path
        # with the friendly landing page (405 + setup guidance) instead of a
        # bare "Method Not Allowed" — both on the direct URL and through the
        # ingress webhook. Guard only the import: the installed server version
        # is user-controlled (channel choice, pip-spec override), so an older
        # ha-mcp without this module must keep serving; the landing is simply
        # absent there, as it is today.
        try:
            from ha_mcp.browser_landing import register_browser_landing
        except ImportError:
            # Older installed ha-mcp: no landing helper to register.
            pass
        else:
            register_browser_landing(server.mcp, self._secret_path)

        # Own the uvicorn server instead of calling mcp.run_async(): cancelling
        # run_async's task does NOT release the listening socket in-process
        # (live-found: the next bring-up failed with EADDRINUSE and uvicorn's
        # lifespan generator raised "athrow(): asynchronous generator is
        # already running"). The CLI never sees this because its process exits.
        # This mirrors fastmcp 3.4.2's run_http_async internals (http_app +
        # uvicorn.Config defaults + _lifespan_manager), pinned by ha-mcp.
        import uvicorn

        # fastmcp >= 3.4.3 ships an on-by-default Host/Origin (DNS-rebinding)
        # guard that would 421 this in-process server's direct LAN listener
        # (bind 0.0.0.0:9584 by default), reached on arbitrary hosts. Default it
        # off to match the CLI / add-on entry points. Guard only the import: a
        # bundled ha-mcp old enough to lack the helper also predates the guard,
        # so there is nothing to disable.
        try:
            from ha_mcp.transport_security import (
                ensure_host_origin_guard_default_off,
            )
        except ImportError:
            # Older bundled ha-mcp: no helper, and no guard to disable either.
            pass
        else:
            ensure_host_origin_guard_default_off()

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

        self._note_startup_phase("starting the HTTP listener")
        stop_task = asyncio.create_task(stop_event.wait())
        async with server.mcp._lifespan_manager():
            serve_task = asyncio.create_task(uv_server.serve())
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

    def _progress_signature(self) -> tuple[int, str]:
        """Snapshot the observable startup progress of the worker thread.

        ``len(sys.modules)`` moves continuously while the worker grinds
        through a cold import (the single longest startup step — minutes on
        slow hardware, issue #1904), and the published phase moves between
        steps. Any change in the pair counts as progress. The module count is
        PROCESS-wide — an approximation: nothing finer-grained is observable
        from outside a thread stuck inside one ``import`` statement, and any
        other HA thread importing concurrently also refreshes the stall
        budget. Erring toward patience is the point; the absolute cap bounds
        the wait regardless.
        """
        return (len(sys.modules), self._startup_phase)

    async def _async_wait_until_ready(self) -> None:
        """Poll a loopback TCP connect until the server accepts, or fail.

        Patience is progress-based: the wait only gives up when there is no
        observable progress for ``_READY_STALL_TIMEOUT_SECONDS`` (or the
        absolute ``_READY_TOTAL_CAP_SECONDS`` ceiling is hit). A slow cold
        import keeps the wait alive; a wedged worker is caught by the stall
        budget on a quiet instance, by the cap at the latest (the progress
        signal is process-wide). On failure stops the thread and raises
        :class:`EmbeddedServerError` so the caller leaves the webhook
        unregistered and files a repair issue.
        """
        start = self._hass.loop.time()
        last_progress = start
        last_signature = self._progress_signature()
        while True:
            if self._thread_exc is not None:
                raise EmbeddedServerError(
                    f"HA-MCP in-process server failed to start: {self._thread_exc}"
                ) from self._thread_exc
            if self._thread is not None and not self._thread.is_alive():
                raise EmbeddedServerError(
                    "HA-MCP in-process server thread exited during startup."
                )
            if await self._async_probe_port():
                _LOGGER.info(
                    "HA-MCP in-process server is listening on %s:%d",
                    self._bind_host,
                    self._port,
                )
                return
            now = self._hass.loop.time()
            signature = self._progress_signature()
            if signature != last_signature:
                last_signature = signature
                last_progress = now
            if now - start >= _READY_TOTAL_CAP_SECONDS:
                failure = (
                    f"HA-MCP in-process server did not become reachable on "
                    f"port {self._port} within {_READY_TOTAL_CAP_SECONDS:.0f}s "
                    f"(last startup phase: {self._startup_phase})."
                )
                break
            if now - last_progress >= _READY_STALL_TIMEOUT_SECONDS:
                failure = (
                    f"HA-MCP in-process server did not become reachable on "
                    f"port {self._port}: no startup progress observed for "
                    f"{_READY_STALL_TIMEOUT_SECONDS:.0f}s (last phase: "
                    f"{self._startup_phase}; {now - start:.0f}s since start)."
                )
                break
            await asyncio.sleep(_READY_POLL_INTERVAL_SECONDS)

        # Gave up — tear the thread down so we never leave a half-started
        # server behind an unregistered webhook.
        await self.async_stop()
        raise EmbeddedServerError(failure)

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


# Version of the ha_mcp generation currently cached in sys.modules — set by
# the worker right after its first import lands, cleared by the purge. Process-wide
# (the module cache it describes is process-wide too). Lets a retry with an
# unchanged install keep the warm cache instead of paying the full cold import
# again (issue #1904).
_CACHED_IMPORT_VERSION: str | None = None


def _purge_ha_mcp_modules() -> None:
    """Drop every cached ``ha_mcp`` module so the next import loads fresh code.

    The in-process server runs as a thread of the HA core Python process, and
    Python resolves imports from the process-wide ``sys.modules`` cache — so
    after a pip install the next worker would silently reuse the OLD code
    unless the cache is purged first. Safe here because ``ha_mcp`` is pure
    Python and is only ever imported inside the (currently stopped) worker
    thread; third-party dependencies are deliberately NOT purged (they are
    shared with the rest of Home Assistant), so a dependency-version change
    still needs an HA core restart.
    """
    global _CACHED_IMPORT_VERSION
    _CACHED_IMPORT_VERSION = None
    # Snapshot the keys: sys.modules can be mutated by concurrent imports on
    # other threads mid-iteration (HA core is heavily threaded).
    purged = [
        name
        for name in list(sys.modules)
        if name == "ha_mcp" or name.startswith("ha_mcp.")
    ]
    if not purged:
        return
    for name in purged:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    _LOGGER.debug("Purged %d cached ha_mcp module(s) before worker start", len(purged))


def _installed_ha_mcp_version(preferred_dist: str | None = None) -> str | None:
    """Return the installed ha-mcp distribution version, or None (blocking).

    Invalidates the import caches first so a just-completed pip install is seen.
    Checks both the stable (``ha-mcp``) and dev (``ha-mcp-dev``) distribution
    names, mirroring ``ha_mcp._version.get_version``. When ``preferred_dist`` is
    provided, checks that channel first so stale metadata from a failed
    best-effort conflicting uninstall cannot mask the package just installed.
    """
    importlib.invalidate_caches()
    # Metadata alone is not proof: a channel switch's best-effort uninstall
    # can leave ORPHANED .dist-info whose files are gone (the shared ha_mcp/
    # tree belongs to whichever dist installed last). Require the import
    # machinery to actually resolve the package before trusting any version.
    if importlib.util.find_spec("ha_mcp") is None:
        return None
    dist_names = (
        (DIST_NAME_DEV, DIST_NAME_STABLE)
        if preferred_dist == DIST_NAME_DEV
        else (DIST_NAME_STABLE, DIST_NAME_DEV)
    )
    for dist_name in dist_names:
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


def _installed_dist_version(dist_name: str) -> str | None:
    """Return the installed version of a SPECIFIC distribution, or None (blocking).

    Invalidates the import caches first so a just-completed (un)install is seen.
    Unlike :func:`_installed_ha_mcp_version` (which reports whichever of the two
    channel distributions is present) this pins the given distribution name, so
    the auto-update check compares the newest PyPI build against the version of
    the channel actually installed.
    """
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _running_ha_mcp_version(channel: str) -> str | None:
    """Return the imported worker version, resolving cross-channel ambiguity."""
    imported_version = getattr(sys.modules.get("ha_mcp"), "__version__", None)
    preferred_dist = dist_for_channel(channel)
    preferred_version = _installed_dist_version(preferred_dist)
    if preferred_version is None:
        return imported_version

    conflicting_dist = (
        DIST_NAME_STABLE if preferred_dist == DIST_NAME_DEV else DIST_NAME_DEV
    )
    conflicting_version = _installed_dist_version(conflicting_dist)
    if imported_version == conflicting_version:
        return preferred_version
    return imported_version


def _uninstall_distribution(dist_name: str, *, target: str | None = None) -> bool:
    """Uninstall a distribution by name (blocking), best-effort.

    Mirrors ``homeassistant.util.package.install_package``'s invocation style —
    ``<python> -m uv pip ...`` with no shell. An explicit dependency target is
    used when Home Assistant installs into ``config/deps``; otherwise ``--python``
    selects Home Assistant's interpreter environment. A failure is logged but
    not raised; the caller treats the cleanup as best-effort.
    """
    args = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "uninstall",
    ]
    if target is not None:
        args += ["--target", os.path.abspath(target)]
    else:
        args += ["--python", sys.executable]
    args.append(dist_name)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_PIP_UNINSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.warning("Could not uninstall %r: %s", dist_name, err)
        return False
    if result.returncode != 0:
        _LOGGER.warning(
            "Uninstall of %r exited %d: %s",
            dist_name,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    return True


def _is_compatible_embedded_version(version: str) -> bool:
    """Return whether a server distribution provides the embedded API."""
    try:
        return Version(version) >= Version(MIN_EMBEDDED_SERVER_VERSION)
    except InvalidVersion:
        return False
