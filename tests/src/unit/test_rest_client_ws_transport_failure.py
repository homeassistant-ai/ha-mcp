"""Unit tests for the fail-loud WebSocket bridge.

``send_websocket_message`` used to catch every exception and return a failure
envelope, which erased the distinction between "HA rejected the command" and
"no answer came back". Callers that must honour the #1624 fail-loud policy
could not recover that distinction from ``error_code`` (the connection classes
carry no ``code``) without matching on message text, so the bridge now raises
``HomeAssistantConnectionError`` on a dead or unresponsive transport and keeps
the envelope for failures Home Assistant actually answered with (issue #1947).

These tests pin the producing half of that contract; the consuming half lives
in ``test_tools_system.py::TestHomeAssistantConnectionErrorPropagation``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)


@pytest.fixture
def client() -> HomeAssistantClient:
    """``HomeAssistantClient`` with stubbed internals, no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        return c


@pytest.fixture
def no_retry_sleep() -> Any:
    """Skip the real 0.5s retry backoff on the 403 path."""
    with patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()) as sleep:
        yield sleep


async def _send_with_failure(
    client: HomeAssistantClient, exc: Exception
) -> dict[str, Any]:
    """Drive ``send_websocket_message`` with a ``send_command`` that raises."""
    ws_client = MagicMock()
    ws_client.send_command = AsyncMock(side_effect=exc)
    with patch(
        "ha_mcp.client.websocket_client.get_websocket_client",
        new=AsyncMock(return_value=ws_client),
    ):
        return await client.send_websocket_message({"type": "config_entries/get"})


async def _send_with_acquire_failure(
    client: HomeAssistantClient, exc: Exception
) -> dict[str, Any]:
    """Drive ``send_websocket_message`` with an unavailable pooled client."""
    with patch(
        "ha_mcp.client.websocket_client.get_websocket_client",
        new=AsyncMock(side_effect=exc),
    ):
        return await client.send_websocket_message({"type": "config_entries/get"})


class TestNoAnswerRaises:
    """A missing answer raises; a rejection HA sent back still returns.

    ``HomeAssistantCommandNotSent`` raises deliberately: it subclasses the
    connection error and means the command provably never left the process, so
    a caller reading its result as authoritative would be wrong for the same
    reason. ``HomeAssistantCommandTimeout`` raises too — a socket that is open
    but has stopped answering leaves the caller just as blind as a closed one.
    Everything HA actually received and rejected stays an envelope, which is
    what keeps callers that degrade on soft failures working.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            HomeAssistantConnectionError("ws gone"),
            HomeAssistantCommandNotSent("WebSocket not authenticated"),
            HomeAssistantCommandTimeout("Command timeout"),
        ],
        ids=["connection_error", "command_not_sent", "command_timeout"],
    )
    @pytest.mark.asyncio
    async def test_no_answer_raises_connection_error(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        with pytest.raises(HomeAssistantConnectionError, match=str(exc)):
            await _send_with_failure(client, exc)

    @pytest.mark.parametrize(
        "exc",
        [
            HomeAssistantCommandError("Unknown command."),
            ValueError("boom"),
        ],
        ids=["command_error", "unexpected"],
    )
    @pytest.mark.asyncio
    async def test_answered_failures_still_return_an_envelope(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        result = await _send_with_failure(client, exc)

        assert result["success"] is False
        assert str(exc) in result["error"]

    @pytest.mark.asyncio
    async def test_connection_error_subtype_is_preserved(
        self, client: HomeAssistantClient
    ) -> None:
        """``HomeAssistantCommandNotSent`` must survive as itself: an
        at-most-once write caller distinguishes "provably never sent" from an
        ambiguous post-send drop, and re-wrapping it into the base class would
        silently downgrade that guarantee."""
        with pytest.raises(HomeAssistantCommandNotSent):
            await _send_with_failure(
                client, HomeAssistantCommandNotSent("WebSocket not authenticated")
            )

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionClosedError(None, None),
            ConnectionResetError("peer reset"),
        ],
        ids=["connection_closed", "connection_reset"],
    )
    @pytest.mark.asyncio
    async def test_socket_write_failures_raise(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        """``send_command`` re-raises the original transport error from the
        send rather than wrapping it, to keep at-most-once semantics for write
        callers, so the bridge sees the library class untranslated. A socket
        that dies mid-request is still a dead transport."""
        with pytest.raises(HomeAssistantConnectionError) as excinfo:
            await _send_with_failure(client, exc)

        # The wrapped original stays reachable: a caller debugging a dead
        # transport needs the library-level cause, not just our class name.
        assert excinfo.value.__cause__ is exc

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("Failed to connect to Home Assistant WebSocket"),
            HomeAssistantConnectionError("ws gone"),
        ],
        ids=["bare_exception", "connection_error"],
    )
    @pytest.mark.asyncio
    async def test_failure_to_acquire_a_client_raises(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        """Never obtaining a usable connection is transport death whatever the
        exception class. The pooled manager raises a bare ``Exception`` when
        ``connect()`` returns False, which is the ordinary "HA is unreachable"
        case, so this cannot be decided on type alone."""
        with pytest.raises(HomeAssistantConnectionError, match=str(exc)):
            await _send_with_acquire_failure(client, exc)

    @pytest.mark.asyncio
    async def test_403_transport_death_raises_after_retries(
        self, client: HomeAssistantClient, no_retry_sleep: Any
    ) -> None:
        """The 403 branch matches on message text, and a connection error can
        carry that text: the class is free to wrap an httpx failure whose
        message spells a 403 as ``Client error '403 Forbidden' for url ...``.
        Deciding by phase and type rather than by the message keeps a dead
        transport failing loud on this path too."""
        with pytest.raises(HomeAssistantConnectionError, match="403 Forbidden"):
            await _send_with_failure(
                client,
                HomeAssistantConnectionError(
                    "HTTP error: Client error '403 Forbidden' for url 'http://ha/api'"
                ),
            )

        assert no_retry_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_403_without_transport_death_still_returns_an_envelope(
        self, client: HomeAssistantClient, no_retry_sleep: Any
    ) -> None:
        """A 403 that HA (or a proxy) answered with is not evidence the
        transport died, so it keeps its suggestions envelope."""
        result = await _send_with_failure(
            client, HomeAssistantCommandError("403 Forbidden")
        )

        assert result["success"] is False
        assert "403 Forbidden" in result["error"]
        assert result["suggestions"]


class TestRenderTemplateBranch:
    """``_handle_render_template`` has its own ``except`` blocks, which used to
    return plain envelopes that ``send_websocket_message`` handed straight
    back — routing a transport death around the classification in the very
    function that implements it."""

    async def _render(
        self, client: HomeAssistantClient, exc: Exception
    ) -> dict[str, Any]:
        ws_client = MagicMock()
        ws_client.send_command_with_event = AsyncMock(side_effect=exc)
        with patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws_client),
        ):
            return await client.send_websocket_message(
                {"type": "render_template", "template": "{{ 1 }}"}
            )

    @pytest.mark.asyncio
    async def test_transport_death_during_render_raises(
        self, client: HomeAssistantClient
    ) -> None:
        with pytest.raises(HomeAssistantConnectionError, match="ws gone"):
            await self._render(client, HomeAssistantConnectionError("ws gone"))

    @pytest.mark.asyncio
    async def test_template_timeout_still_returns_an_envelope(
        self, client: HomeAssistantClient
    ) -> None:
        """The event wait here is the caller's own template timeout (3s by
        default), not the 30s round-trip budget: a template that is merely slow
        to render leaves the socket healthy, so it stays a soft failure."""
        result = await self._render(client, TimeoutError())

        assert result["success"] is False
        assert "Event timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_template_error_still_returns_an_envelope(
        self, client: HomeAssistantClient
    ) -> None:
        result = await self._render(client, ValueError("bad template"))

        assert result["success"] is False
        assert "bad template" in result["error"]
