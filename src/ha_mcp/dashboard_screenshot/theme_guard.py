"""Snapshot and restore the engine user's saved frontend theme (issue #1909).

NOTE: This guard is currently disabled at its call site (``capture.py``)
because upstream Puppet fixed the cold-render ``settheme`` dispatch that made
the bracket necessary (#1991). The code here is retained unchanged so the
bracket can be re-enabled by uncommenting the snapshot/restore calls in
``capture.py`` if a future engine regression reintroduces the write.

Stock Puppet dispatches Home Assistant's ``settheme`` event on every
cold-browser render — its ``dark`` query flag is presence-based, so "not
requested" reaches the frontend as an explicit "light". Home Assistant
persists that selection server-side per user (``frontend/set_user_data``,
key ``"theme"``) and syncs it to every session of the user whose long-lived
token the engine runs with. A plain screenshot call therefore flips a
dark-mode user's real web and mobile UI to light.

ha-mcp cannot suppress the engine's write, so every capture batch is
bracketed instead: read the engine user's saved theme before rendering and
write it back afterwards when the render changed it (an unchanged value is
never rewritten).

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
    return EngineCredential(url=base_url, token=token)


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

    @classmethod
    def for_capture(
        cls,
        addon_credential: EngineCredential | None,
        client: Any,
    ) -> ThemeGuard:
        """Resolve the engine user's credential for one capture batch."""
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
        ws = HomeAssistantWebSocketClient(self.credential.url, self.credential.token)
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
            "frontend/get_user_data", key=THEME_USER_DATA_KEY
        )
        payload = response.get("result") if isinstance(response, dict) else None
        return payload.get("value") if isinstance(payload, dict) else None

    async def take_snapshot(self) -> None:
        """Record the saved theme before the engine renders. Never raises."""
        if self.credential is None:
            return
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

    async def restore(self) -> None:
        """Write the snapshot back if the render changed it. Never raises."""
        if not self._snapshot_taken or self.credential is None:
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
