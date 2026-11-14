"""Errors the Home Assistant clients raise."""

from typing import Any


class HomeAssistantError(Exception):
    """Base exception for Home Assistant API errors."""


class HomeAssistantConnectionError(HomeAssistantError):
    """Connection error to Home Assistant."""


class HomeAssistantCommandNotSent(HomeAssistantConnectionError):
    """A WS command that provably never left the process.

    Raised by ``HomeAssistantWebSocketClient.send_command``'s entry guard (socket not
    authenticated) and by ``send_websocket_message`` when no attempt obtained a client
    (raised from the acquisition error, so a rejected token survives as ``__cause__``).
    A subclass of ``HomeAssistantConnectionError``, so broad handlers are unaffected,
    yet a write consumer can catch it FIRST: the write provably never happened, so a
    legacy first fire cannot double-apply. Check ``__cause__`` before such a fallback:
    resending a rejected token over REST adds a 401 toward Home Assistant's IP ban. A
    ``send_json_message`` failure is NOT this type: ``websocket.send()`` raising does
    not prove the frame was untransmitted (bytes may already be on the socket when the
    close surfaces), so send_command re-raises the original exception and the consumer
    treats it as ambiguous. A post-send socket close (mid-await) likewise raises a plain
    ``HomeAssistantConnectionError`` (the close handler sets it on the pending future),
    so type alone distinguishes never-sent from sent-then-dropped/ambiguous.
    """


class HomeAssistantAuthError(HomeAssistantError):
    """Authentication error with Home Assistant.

    Sibling of ``HomeAssistantAPIError`` (not a subclass). The codebase has
    18 ``except HomeAssistantAPIError`` sites (ws_waiters polling,
    tools_integrations registry lookups, etc.) that deliberately rely on
    auth errors NOT matching so they can propagate to a paired
    ``except (HomeAssistantConnectionError, HomeAssistantAuthError): raise``
    block. Subclassing AuthError under APIError silently swallowed those
    auth errors as part of the local "this entity is not registered yet"
    polling logic. Sites that specifically need to catch both must list
    them explicitly (see ``_get_supervisor_log`` and
    ``_get_system_service_log`` in ``log_sources_supervisor.py``).
    """


class HomeAssistantAdminRequiredError(HomeAssistantAuthError):
    """An admin-only request refused because the token's user is not an admin.

    A REST request is refused locally, never sent: Home Assistant would answer
    401 and count it toward its IP ban (#2546). A service call is refused by
    Home Assistant over WebSocket, where a refusal is not counted.
    """


class HomeAssistantAPIError(HomeAssistantError):
    """API error from Home Assistant."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class HomeAssistantCommandError(HomeAssistantError):
    """WebSocket command returned success=False.

    Raised by ``WebSocketClient.send_command`` when Home Assistant responds
    with ``{type: "result", success: False}``. Used as a type marker in
    ``_classify_exception``'s match dispatch; classification then falls
    through to ``_classify_by_message`` for pattern matching on the
    error message.

    ``code`` carries HA's structured WebSocket error code (the ``code`` field
    of the response ``error`` object — e.g. ``"unknown_command"``,
    ``"invalid_format"``) when available, so routing decisions can key off the
    stable code rather than pattern-matching the human-readable message. It
    defaults to ``None`` for exceptions raised without a code (older raise
    sites, string error payloads, or direct construction in tests), which
    keeps the single-positional-argument constructor backward-compatible.
    """

    def __init__(self, message: str, code: str | None = None, *, error: Any = None):
        super().__init__(message)
        self.code = code
        # Core's own name for the error, when its error object carried one.
        details = error if isinstance(error, dict) else {}
        self.translation_domain: str | None = details.get("translation_domain")
        self.translation_key: str | None = details.get("translation_key")


class HomeAssistantCommandTimeout(HomeAssistantError):
    """WebSocket ``send_command`` timed out waiting for HA's response.

    Sibling of ``HomeAssistantCommandError`` (not a subclass) so existing
    ``except HomeAssistantCommandError`` sites — including the match
    dispatch in ``helpers._classify_exception`` — keep their original
    semantics. Callers that specifically want to handle the WS
    round-trip timeout (e.g. short-lived waiter cleanup that should
    swallow a timeout instead of masking the real wait result) catch
    this type directly. Replaces a bare ``Exception("Command timeout")``
    string-match pattern (#1382 Patch76 review).
    """
