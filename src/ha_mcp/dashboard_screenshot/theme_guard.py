"""Detect when the screenshot engine changes the engine user's saved theme.

Puppet dispatches Home Assistant's ``settheme`` event on cold-browser
renders. Its ``dark`` query flag is presence-based, so "not requested"
reaches the frontend as an explicit "light". Home Assistant persists that
selection server-side per user (``frontend/set_user_data``, key ``"theme"``)
and syncs it to every session of the user whose long-lived token the engine
runs with, so a screenshot flips that user's real web and mobile UI (#1909).

Upstream stopped the dispatch for renders requesting nothing
(balloob/home-assistant-addons#89), but by its own title only for that case,
and it is unreleased as of Puppet 2.6.0 -- so on current releases every
render writes.

**This guard never writes.** It reads the saved theme before the batch and
again afterwards, and when the render changed it, reports the previous value
so the agent can restore it with ``ha_manage_theme`` -- a tool correctly
annotated as a write. That keeps the screenshot and dashboard-get tools
honestly ``readOnlyHint: True`` (#1991, PR #2014) while still surfacing the
damage. Because nothing is written, concurrent batches cannot corrupt each
other and no serialization is needed.

Pointing the engine at its own dedicated Home Assistant user avoids the
problem outright: the write then lands on an account nobody looks at.

Credential resolution mirrors engine discovery:

- **HA OS / Supervised** -- the Puppet add-on's own ``access_token`` and
  ``home_assistant_url`` options, taken from the Supervisor add-on info that
  engine discovery already fetches. The token lives only in process memory
  and is never logged or returned.
- **Docker / standalone / OAuth / embedded** -- ha-mcp's direct Home
  Assistant credentials, which match whenever the engine runs with a token
  for the same user (the common single-user setup).
- Anything else -- detection stays inactive and captures behave as before.
"""

from __future__ import annotations

import asyncio
import ipaddress
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

# Sentinel: None is a legitimate stored theme value, so it cannot mean "unset".
_UNSET: Any = object()

# Where Puppet reaches Home Assistant when its ``home_assistant_url`` option
# is unset — the Supervisor-internal alias, mirrored from the add-on default.
DEFAULT_ENGINE_HA_URL = "http://homeassistant:8123"

# Hosts the Supervisor-internal / loopback routes use. Cleartext to these
# never leaves the host or its container network, which SECURITY.md names as
# the trusted zone for standard mode. Cleartext to anything else would put the
# engine account's bearer token on the wire for an on-path attacker, so it is
# refused rather than sent.
_LOCAL_HOSTS = frozenset(
    {"homeassistant", "localhost", "supervisor", "127.0.0.1", "::1", "[::1]"}
)


def _refuses_cleartext(url: str) -> bool:
    """True when ``url`` would send the token in cleartext to a remote host."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return False
    # Private ranges stay on the local network, the documented trusted zone --
    # but ONLY as real IP literals. A DNS name that merely looks like one
    # ("10.attacker.example") resolves wherever its owner points it, so string
    # prefix matching would hand the token to an external host.
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local)


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

# The guard runs before the engine is contacted and must never eat the
# caller's MCP timeout window; send_command otherwise waits 30s by default.
COMMAND_TIMEOUT_SECONDS = 5.0

# COMMAND_TIMEOUT_SECONDS only bounds a command once the socket is open and
# authenticated. An unreachable endpoint or a bad token stalls in connect/auth
# instead, before the engine is ever contacted, so the whole session is bounded
# too.
SESSION_TIMEOUT_SECONDS = 10.0

# Cleanup runs shielded so a cancellation cannot abort the close mid-flight,
# but shielded work still has to be bounded or a blocked disconnect() would
# extend the session past SESSION_TIMEOUT_SECONDS. The shielded task keeps
# running in the background when this bound expires; we just stop waiting.
CLOSE_TIMEOUT_SECONDS = 2.0


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
    # default, or every session fails and no change is ever detected.
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
    changed_from: Any = None
    # False when the credential is ha-mcp's own rather than the engine's own
    # token: it MAY be the same Home Assistant user (the common single-user
    # setup) but that cannot be established, so the report says so.
    credential_is_engine: bool = True

    @classmethod
    def for_capture(
        cls,
        addon_credential: EngineCredential | None,
        client: Any,
    ) -> ThemeGuard:
        """Resolve the engine user's credential for one capture batch."""
        credential = addon_credential or _client_credential(client)
        # Only the add-on's own token provably belongs to the engine. The
        # client fallback MAY be the same user (the common single-user setup)
        # but that cannot be established, so the report says so rather than
        # implying the engine account was the one observed.
        provably_engine = addon_credential is not None
        if credential is None:
            logger.debug(
                "Dashboard theme guard inactive: no engine credential is "
                "discoverable in this deployment"
            )
        return cls(credential=credential, credential_is_engine=provably_engine)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[HomeAssistantWebSocketClient]:
        """Yield a short-lived authenticated WebSocket as the engine user."""
        from ..client.websocket_client import HomeAssistantWebSocketClient

        assert self.credential is not None
        if _refuses_cleartext(self.credential.url):
            raise ConnectionError(
                "refusing to send the screenshot engine's token in cleartext "
                f"to a remote host ({self.credential.url}); use https://"
            )
        ws = HomeAssistantWebSocketClient(
            self.credential.url,
            self.credential.token,
            verify_ssl=self.credential.verify_ssl,
        )
        # connect() is INSIDE the try: SESSION_TIMEOUT_SECONDS can cancel
        # mid-connect or mid-auth, and CancelledError is a BaseException, so
        # leaving it outside skipped disconnect() entirely and stranded the
        # socket plus its background reader task.
        try:
            if not await ws.connect():
                reason = ws.last_connect_error
                detail = f": {reason}" if isinstance(reason, str) else ""
                raise ConnectionError(
                    f"could not authenticate to {self.credential.url}{detail}"
                )
            yield ws
        finally:
            # Best-effort: a real failure to close must not mask the original
            # exception (including the cancellation that triggered cleanup).
            # Owned explicitly: a bare shield leaves the inner task pending
            # after a timeout or cancellation, still holding the socket and
            # able to raise late with nobody to receive it.
            close = asyncio.ensure_future(ws.disconnect())
            try:
                # shield: we are frequently here *because* of a cancellation
                # (SESSION_TIMEOUT_SECONDS). A bare await would be cancelled
                # at once and the socket would never actually close.
                await asyncio.wait_for(
                    asyncio.shield(close), timeout=CLOSE_TIMEOUT_SECONDS
                )
            except (Exception, TimeoutError) as close_error:
                logger.debug(
                    "Ignoring error while closing the theme-guard session: %s",
                    close_error,
                )
            finally:
                if not close.done():
                    close.cancel()
                # Retrieve the outcome so a late failure never surfaces as an
                # orphaned "exception was never retrieved" warning.
                await asyncio.gather(close, return_exceptions=True)

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

    async def _read_theme(self) -> Any:
        """One bounded read of the engine user's saved theme."""
        async with self._session() as ws:
            return await self._fetch_theme(ws)

    async def take_snapshot(self) -> None:
        """Record the saved theme before the engine renders. Never raises."""
        if self.credential is None:
            return
        try:
            self._snapshot = await asyncio.wait_for(
                self._read_theme(), timeout=SESSION_TIMEOUT_SECONDS
            )
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

    async def detect_change(self) -> None:
        """Report -- never repair -- a theme the render changed. Never raises.

        Writing the value back here would make the screenshot tools issue
        ``frontend/set_user_data``, which is exactly what disqualifies them
        from ``readOnlyHint: True``. Instead the previous value is surfaced
        as a warning naming the write-annotated tool that can restore it.
        """
        if not self._snapshot_taken or self.credential is None:
            return
        try:
            # Puppet's settheme dispatch happens during page navigation, but
            # the frontend's resulting user-data write is asynchronous -- let
            # it land before reading (see RESTORE_SETTLE_SECONDS).
            await asyncio.sleep(RESTORE_SETTLE_SECONDS)
            current = await asyncio.wait_for(
                self._read_theme(), timeout=SESSION_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning(
                "Could not re-read the screenshot engine user's saved theme "
                "after rendering: %s",
                exc,
            )
            self.warnings.append(
                "Could not check whether the screenshot render changed the "
                "saved frontend theme of the engine token's user; "
                f"{_RESTORE_HINT}."
            )
            return
        if current == self._snapshot:
            return
        self.changed_from = self._snapshot
        # A never-configured baseline is restored as {} rather than null:
        # live frontend sessions ignore a null subscription push (they stay
        # flipped until reload), while an empty settings object re-applies
        # default/auto behavior immediately and means the same thing on the
        # next frontend boot.
        restore_value = self._snapshot if self._snapshot is not None else {}
        logger.info(
            "Screenshot render changed the engine user's saved theme "
            "(was %s, now %s); reporting for agent-side restore",
            self._snapshot,
            current,
        )
        if self.credential_is_engine:
            remedy = (
                "To restore it, call ha_manage_theme(action="
                f"'set_engine_theme', value={restore_value!r}, "
                f"expected_current={current!r}) -- that guard re-checks the "
                "stored theme immediately before writing and skips the write "
                "if it changed."
            )
        else:
            # Do NOT name ha_manage_theme here: this branch is reached via the
            # client-credential fallback, which is the same condition under
            # which the engine-theme actions refuse. Recommending them would
            # send the agent at a command that cannot succeed.
            remedy = (
                "This was observed with ha-mcp's own Home Assistant "
                "credential rather than the engine's own token, so ha-mcp "
                "cannot confirm which account it belongs to and will not act "
                "on it. Restore it from that account's own session: Profile "
                "> General in the Home Assistant UI."
            )
        self.warnings.append(
            "The screenshot engine changed the saved frontend theme of the "
            "account its token belongs to, which also changes that account's "
            "live web and mobile sessions. This tool is read-only and will "
            f"not change it back. {remedy} To stop this happening at all, "
            "give the screenshot engine its own Home Assistant user and "
            "long-lived token, so its writes land on an account nobody "
            "looks at."
        )


async def read_engine_theme(credential: EngineCredential) -> Any:
    """Read the engine user's saved ``theme`` frontend user-data value."""
    guard = ThemeGuard(credential=credential)
    return await asyncio.wait_for(guard._read_theme(), timeout=SESSION_TIMEOUT_SECONDS)


class ThemeChangedError(RuntimeError):
    """The saved theme is not what the caller expected to overwrite."""

    def __init__(self, actual: Any) -> None:
        super().__init__("saved theme no longer matches expected_current")
        self.actual = actual


async def write_engine_theme(
    credential: EngineCredential,
    value: Any,
    expected_current: Any = _UNSET,
    *,
    force: bool = False,
) -> None:
    """Write the engine user's saved ``theme`` frontend user-data value.

    Only reached through ``ha_manage_theme``, which is annotated as a write.
    Unless ``force`` is set the stored value is re-read
    immediately before the write and the write is skipped on a mismatch. This
    is best-effort, not atomic: ``frontend/set_user_data`` is an unconditional
    ``async_set_item`` with no version or etag, so Home Assistant offers
    nothing to compare against server-side and a change landing between the
    re-read and the write is not caught. What it does catch is what actually
    happens -- a delayed restore against a theme the user has since changed,
    and a misresolved credential, whose account's theme will not match either.
    """
    guard = ThemeGuard(credential=credential)

    async def _write() -> None:
        async with guard._session() as ws:
            if not force:
                current = await ThemeGuard._fetch_theme(ws)
                expected = None if expected_current is _UNSET else expected_current
                if current != expected:
                    raise ThemeChangedError(current)
            await ws.send_command(
                "frontend/set_user_data",
                key=THEME_USER_DATA_KEY,
                value=value,
                _wait_timeout=COMMAND_TIMEOUT_SECONDS,
            )

    await asyncio.wait_for(_write(), timeout=SESSION_TIMEOUT_SECONDS)
