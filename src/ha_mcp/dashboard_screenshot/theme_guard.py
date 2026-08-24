"""Snapshot and restore the engine user's saved frontend theme (issue #1909).

Puppet dispatches Home Assistant's ``settheme`` event for renders that ask
for a theme. Home Assistant persists that selection server-side per user
(``frontend/set_user_data``, key ``"theme"``) and syncs it to every session
of the user whose long-lived token the engine runs with, so such a
screenshot flips that user's real web and mobile UI.

Upstream stopped the dispatch for renders that request nothing
(balloob/home-assistant-addons#89), but by its own title only for that case —
an explicit ``theme=``/``dark`` render still writes on every engine version.

ha-mcp cannot suppress the engine's write, so themed capture batches are
bracketed instead: read the engine user's saved theme before rendering and
write it back afterwards when the render changed it (an unchanged value is
never rewritten). Unthemed batches are not bracketed and issue no writes,
keeping the screenshot tools honestly read-only (#1991).

Credential resolution mirrors engine discovery:

- **HA OS / Supervised** — the Puppet add-on's own ``access_token`` and
  ``home_assistant_url`` options, taken from the Supervisor add-on info that
  engine discovery already fetches. The token lives only in process memory
  for the duration of one capture batch and is never logged or returned.
- **Docker / standalone / OAuth / embedded** — ha-mcp's direct Home
  Assistant credentials. These protect the user whenever the sidecar engine
  runs with a token for the same user (the common single-user setup).
- Anything else (e.g. Supervisor-proxy auth with no discoverable engine
  token) — the guard stays inactive and captures behave as before.

Guard failures are always non-fatal: screenshots must keep working even
when the theme cannot be protected. A snapshot or restore that was
*attempted* but failed surfaces as a tool-response warning.

Known limit: if a real session changes the user's theme during the few
seconds of a capture batch, the restore reverts that change too — the guard
cannot tell the engine's write apart from a concurrent human one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .._version import is_running_in_addon

if TYPE_CHECKING:
    from ..client.websocket_client import HomeAssistantWebSocketClient

logger = logging.getLogger(__name__)

THEME_USER_DATA_KEY = "theme"

# Where Puppet reaches Home Assistant when its ``home_assistant_url`` option
# is unset — the Supervisor-internal alias, mirrored from the add-on default.
DEFAULT_ENGINE_HA_URL = "http://homeassistant:8123"

_RESTORE_HINT = (
    "the engine token's user can re-select their theme under Profile > "
    "General in the Home Assistant UI"
)

# The engine returns the image as soon as the render settles, but the
# frontend's settheme handler saves the user data asynchronously — with a
# very low wait_ms the write can land after the HTTP response. Wait this
# long before the post-capture read so it observes the engine's write
# instead of racing it (a stale read would compare equal, skip the
# restore, and let the late write survive).
RESTORE_SETTLE_SECONDS = 1.0

# The guard is best-effort and runs *before* the engine is contacted, so it
# must never eat the caller's MCP timeout window. send_command defaults to a
# 30s wait; a guard that hangs that long would stop a healthy engine ever
# returning an image.
COMMAND_TIMEOUT_SECONDS = 5.0

# Longest a batch waits for another batch's bracket to finish. The guard is
# best-effort, so a wedged holder must degrade to an unguarded render rather
# than block the capture indefinitely.
LOCK_WAIT_SECONDS = 5.0

# Snapshot-through-restore must be serialized per engine user. Two concurrent
# batches would otherwise interleave as: A renders and clobbers dark->light,
# B snapshots that transient light, A restores dark, B restores light —
# leaving the user permanently on the clobbered value.
#
# Keyed by running event loop as well as engine user: an asyncio.Lock binds
# to the loop that first awaits it and raises if reused from another, so a
# process-global-by-credential dict would break any second loop (and leaks
# into unit tests, which get a fresh loop each). The server runs one loop, so
# in production this holds exactly one entry per engine user.
_ENGINE_LOCKS: dict[tuple[int, str, str], asyncio.Lock] = {}


def _engine_lock(credential: EngineCredential) -> asyncio.Lock:
    """Return this loop's lock for one engine user."""
    key = (id(asyncio.get_running_loop()), credential.url, credential.token)
    lock = _ENGINE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ENGINE_LOCKS[key] = lock
    return lock


@dataclass(frozen=True, slots=True)
class EngineCredential:
    """Home Assistant URL + token that authenticate as the engine's user.

    Deliberately carries only the two values the theme guard needs, so the
    engine add-on's raw Supervisor options (a secret-bearing dict) never
    cross module boundaries. The token must never be logged or surfaced in
    responses.
    """

    url: str
    token: str
    verify_ssl: bool | None = None


def addon_credential_from_options(
    addon_options: Mapping[str, Any] | None,
) -> EngineCredential | None:
    """Extract the engine user's credential from Puppet add-on options."""
    if not addon_options:
        return None
    token = str(addon_options.get("access_token") or "").strip()
    if not token:
        return None
    url = str(addon_options.get("home_assistant_url") or "").strip()
    return EngineCredential(url=url or DEFAULT_ENGINE_HA_URL, token=token)


def _client_credential(client: Any) -> EngineCredential | None:
    """Fall back to ha-mcp's own direct Home Assistant credential.

    Only meaningful outside add-on mode: the Supervisor proxy authenticates
    as the Supervisor system user, whose frontend profile is unrelated to
    the engine token's user, so protecting it would be a silent no-op.
    Embedded mode is deliberately NOT excluded — there the HA core container
    carries ``SUPERVISOR_TOKEN`` but the server is a plain admin client with
    a real user token, exactly what this fallback needs.
    """
    if is_running_in_addon():
        return None
    base_url = str(getattr(client, "base_url", "") or "").strip()
    token = str(getattr(client, "token", "") or "").strip()
    if not base_url.startswith(("http://", "https://")) or not token:
        return None
    # Carry the client's own TLS setting: a direct client built with
    # verify_ssl=False (self-signed HA) must not fall back to the global
    # default here, or every guard session fails and the theme stays
    # clobbered while the render itself succeeds.
    verify_ssl = getattr(client, "verify_ssl", None)
    return EngineCredential(
        url=base_url,
        token=token,
        verify_ssl=verify_ssl if isinstance(verify_ssl, bool) else None,
    )


@dataclass
class ThemeGuard:
    """Per-capture-batch snapshot/restore of the engine user's saved theme.

    ``credential`` and ``warnings`` are the public contract; the snapshot
    pair is internal lifecycle state driven only by :meth:`take_snapshot`
    and :meth:`restore`.
    """

    credential: EngineCredential | None
    warnings: list[str] = field(default_factory=list)
    _snapshot: Any = None
    _snapshot_taken: bool = False
    _lock: Any = None

    @classmethod
    def for_capture(
        cls,
        addon_credential: EngineCredential | None,
        client: Any,
        *,
        armed: bool = True,
    ) -> ThemeGuard:
        """Resolve the engine user's credential for one capture batch.

        ``armed=False`` yields an inert guard: Puppet only writes the theme
        for renders that request one, so unthemed captures need no bracket
        and stay free of any write (#1991).
        """
        if not armed:
            return cls(credential=None)
        credential = addon_credential or _client_credential(client)
        if credential is None:
            logger.debug(
                "Dashboard theme guard inactive: no engine credential is "
                "discoverable in this deployment"
            )
        return cls(credential=credential)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[HomeAssistantWebSocketClient]:
        """Yield a short-lived authenticated WebSocket as the engine user."""
        from ..client.websocket_client import HomeAssistantWebSocketClient

        assert self.credential is not None
        ws = HomeAssistantWebSocketClient(
            self.credential.url,
            self.credential.token,
            verify_ssl=self.credential.verify_ssl,
        )
        if not await ws.connect():
            reason = ws.last_connect_error
            detail = f": {reason}" if isinstance(reason, str) else ""
            raise ConnectionError(
                f"could not authenticate to {self.credential.url}{detail}"
            )
        try:
            yield ws
        finally:
            await ws.disconnect()

    @staticmethod
    async def _fetch_theme(ws: HomeAssistantWebSocketClient) -> Any:
        """Read the persisted ``theme`` frontend user-data value (may be None)."""
        response = await ws.send_command(
            "frontend/get_user_data",
            key=THEME_USER_DATA_KEY,
            _wait_timeout=COMMAND_TIMEOUT_SECONDS,
        )
        payload = response.get("result") if isinstance(response, dict) else None
        return payload.get("value") if isinstance(payload, dict) else None

    async def take_snapshot(self) -> None:
        """Record the saved theme before the engine renders. Never raises.

        Holds the per-engine-user lock until :meth:`restore` releases it, so
        two overlapping batches cannot interleave snapshot and restore.
        """
        if self.credential is None:
            return
        # Acquired before the read so the whole snapshot->render->restore
        # window is exclusive for this engine user.
        lock = _engine_lock(self.credential)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_SECONDS)
            self._lock = lock
        except TimeoutError:
            # Proceed unguarded rather than stall the render; the concurrent
            # batch holding the lock is doing its own restore.
            logger.warning(
                "Timed out waiting for another screenshot batch's theme "
                "bracket; rendering without serialization"
            )
        try:
            async with self._session() as ws:
                self._snapshot = await self._fetch_theme(ws)
            self._snapshot_taken = True
        except Exception as exc:
            logger.warning(
                "Could not read the screenshot engine user's saved theme "
                "before rendering: %s",
                exc,
            )
            self.warnings.append(
                "Could not read the screenshot engine user's saved frontend "
                f"theme before rendering; if the render changed it, {_RESTORE_HINT}."
            )
            # Nothing to restore, so do not hold the lock across the render.
            self._release_lock()

    def _release_lock(self) -> None:
        """Drop the per-engine lock if this guard holds it."""
        lock = self._lock
        self._lock = None
        if lock is not None and lock.locked():
            lock.release()

    async def restore(self) -> None:
        """Write the snapshot back if the render changed it. Never raises."""
        if not self._snapshot_taken or self.credential is None:
            self._release_lock()
            return
        try:
            # Puppet's settheme dispatch happens during page navigation, but
            # the frontend's resulting user-data write is asynchronous — let
            # it land before reading (see RESTORE_SETTLE_SECONDS).
            await asyncio.sleep(RESTORE_SETTLE_SECONDS)
            async with self._session() as ws:
                current = await self._fetch_theme(ws)
                if current != self._snapshot:
                    # A never-configured baseline must restore as {} rather
                    # than null: live frontend sessions ignore a null
                    # subscription push (they would stay flipped until
                    # reload), while an empty settings object re-applies
                    # default/auto behavior immediately and means the same
                    # thing on the next frontend boot.
                    restore_value = self._snapshot if self._snapshot is not None else {}
                    await ws.send_command(
                        "frontend/set_user_data",
                        key=THEME_USER_DATA_KEY,
                        value=restore_value,
                        _wait_timeout=COMMAND_TIMEOUT_SECONDS,
                    )
        except Exception as exc:
            logger.warning(
                "Could not restore the screenshot engine user's saved theme "
                "after rendering: %s",
                exc,
            )
            self.warnings.append(
                "The screenshot render may have changed the saved frontend "
                "theme of the engine token's user and restoring it failed; "
                f"{_RESTORE_HINT}."
            )
        finally:
            self._release_lock()
