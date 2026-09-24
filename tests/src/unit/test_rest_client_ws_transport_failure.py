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

from ha_mcp._vendor.websockets.exceptions import ConnectionClosedError
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
            Exception("Lock not initialized"),
            HomeAssistantConnectionError(
                "Failed to connect to Home Assistant WebSocket"
            ),
        ],
        ids=["untyped", "connection_error"],
    )
    @pytest.mark.asyncio
    async def test_failure_to_acquire_a_client_raises(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        """Never obtaining a usable connection counts as transport death
        whatever the exception class, which is why the phase is checked before
        the type.

        The failed-connect raise is typed now, but the acquisition path still
        has untyped exits — ``WebSocketManager`` guards its pool lock with a
        bare ``Exception("Lock not initialized")`` — and a caller cannot read a
        result it never got regardless of how the manager chose to signal that.
        The untyped case is what fails if the phase check is ever dropped in
        favour of the type check alone."""
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
        ws_client.subscribe_command = AsyncMock(side_effect=exc)
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
        assert result["client_error"] is True


def _event(sub_id: int, **event: Any) -> dict[str, Any]:
    return {"id": sub_id, "type": "event", "event": event}


class TestRenderTemplateVerdict:
    """What ``render_template`` returns for the frame sequences HA really sends.

    Each case replays the events captured from Home Assistant 2026.9.3 for that
    template (#2522); the result frame itself is consumed by
    ``subscribe_command``, so only the events reach the queue.
    """

    async def _render(
        self,
        client: HomeAssistantClient,
        events: list[dict[str, Any]],
        **message: Any,
    ) -> tuple[dict[str, Any], MagicMock]:
        import asyncio

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in events:
            queue.put_nowait(event)
        ws_client = MagicMock()
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.release_subscription = AsyncMock()
        with patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws_client),
        ):
            result = await client.send_websocket_message(
                {"type": "render_template", "template": "t", "timeout": 1, **message}
            )
        return result, ws_client

    @pytest.mark.asyncio
    async def test_result_is_returned_and_the_subscription_released(
        self, client: HomeAssistantClient
    ) -> None:
        result, ws = await self._render(
            client, [_event(7, result=2, listeners={"all": False})]
        )
        assert result == {
            "success": True,
            "result": 2,
            "template": "t",
            "listeners": {"all": False},
        }
        ws.release_subscription.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_error_event_before_the_result_frame_is_the_error(
        self, client: HomeAssistantClient
    ) -> None:
        result, ws = await self._render(
            client,
            [
                _event(7, error="ZeroDivisionError: division by zero", level="ERROR"),
                _event(7, error="ZeroDivisionError: division by zero", level="ERROR"),
            ],
        )
        assert result["success"] is False
        assert result["error"] == "ZeroDivisionError: division by zero"
        ws.release_subscription.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_the_class_named_error_is_preferred(
        self, client: HomeAssistantClient
    ) -> None:
        bare = "'dict object' has no attribute 'split'"
        result, _ = await self._render(
            client,
            [
                _event(7, error=bare, level="ERROR"),
                _event(7, error=f"UndefinedError: {bare}", level="ERROR"),
            ],
        )
        assert result["error"] == f"UndefinedError: {bare}"

    @pytest.mark.asyncio
    async def test_warnings_ride_along_with_the_result(
        self, client: HomeAssistantClient
    ) -> None:
        warning = "'undefined_var' is undefined"
        result, _ = await self._render(
            client,
            [
                _event(7, error=warning, level="WARNING"),
                _event(7, error=warning, level="WARNING"),
                _event(7, result="x", listeners={}),
            ],
        )
        assert result["success"] is True
        assert result["result"] == "x"
        assert result["warnings"] == [warning]

    @pytest.mark.asyncio
    async def test_warnings_before_an_error_are_kept(
        self, client: HomeAssistantClient
    ) -> None:
        result, _ = await self._render(
            client,
            [
                _event(7, error="'a' is undefined", level="WARNING"),
                _event(7, error="ZeroDivisionError: division by zero", level="ERROR"),
            ],
        )
        assert result["success"] is False
        assert result["error"] == "ZeroDivisionError: division by zero"
        assert result["warnings"] == ["'a' is undefined"]

    @pytest.mark.asyncio
    async def test_unrecognised_events_are_skipped(
        self, client: HomeAssistantClient
    ) -> None:
        result, _ = await self._render(
            client,
            [_event(7, error={"not": "a string"}), _event(7, result=1, listeners={})],
        )
        assert result["success"] is True
        assert result["result"] == 1

    @pytest.mark.asyncio
    async def test_strict_and_variables_reach_home_assistant(
        self, client: HomeAssistantClient
    ) -> None:
        _, ws = await self._render(
            client,
            [_event(7, result=42, listeners={})],
            strict=True,
            variables={"foo": 21},
        )
        kwargs = ws.subscribe_command.await_args.kwargs
        assert kwargs["strict"] is True
        assert kwargs["variables"] == {"foo": 21}
        assert kwargs["report_errors"] is True
        # The template's own timeout travels as a command field; the wait for
        # Home Assistant's acknowledgement is a separate budget.
        assert kwargs["timeout"] == 1
        assert kwargs["wait_timeout"] == 3

    @pytest.mark.asyncio
    async def test_no_verdict_without_report_errors_says_why(self) -> None:
        import asyncio

        from ha_mcp.client.rest_client import (
            RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
            _read_render_verdict,
        )

        result = await _read_render_verdict(asyncio.Queue(), "t", 0.01, False)
        assert result == {
            "success": False,
            "error": RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
            "no_verdict": True,
            "template": "t",
        }

    @pytest.mark.asyncio
    async def test_lost_verdict_with_report_errors_is_an_event_timeout(self) -> None:
        import asyncio

        from ha_mcp.client.rest_client import _read_render_verdict

        result = await _read_render_verdict(asyncio.Queue(), "t", 0.01, True)
        assert result["error"] == "Event timeout - template result not received"
        assert result["no_verdict"] is True

    @pytest.mark.asyncio
    async def test_closed_socket_is_a_connection_error(self) -> None:
        import asyncio

        from ha_mcp.client.rest_client import _read_render_verdict

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        queue.shutdown(immediate=True)
        with pytest.raises(HomeAssistantConnectionError):
            await _read_render_verdict(queue, "t", 1.0, True)

    @pytest.mark.asyncio
    async def test_rejected_command_keeps_home_assistant_message(
        self, client: HomeAssistantClient
    ) -> None:
        ws_client = MagicMock()
        ws_client.subscribe_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "subscribe_command('render_template') failed: "
                "Exceeded maximum execution time of 1.0s",
                "template_error",
            )
        )
        with patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws_client),
        ):
            result = await client.send_websocket_message(
                {"type": "render_template", "template": "t", "timeout": 1}
            )
        assert result["error"] == "Exceeded maximum execution time of 1.0s"
        assert result["error_code"] == "template_error"
