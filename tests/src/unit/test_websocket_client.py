"""Unit tests for WebSocket client URL construction.

These tests verify that the WebSocket client correctly constructs WebSocket URLs
for both standard Home Assistant installations and Supervisor proxy environments.
"""

import asyncio

import pytest


class TestWebSocketURLConstruction:
    """Tests for WebSocket URL construction logic."""

    def test_standard_http_url_produces_ws_api_websocket(self):
        """Standard HTTP URL should produce ws://host:port/api/websocket."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://homeassistant.local:8123/api/websocket"

    def test_standard_https_url_produces_wss_api_websocket(self):
        """Standard HTTPS URL should produce wss://host:port/api/websocket."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://homeassistant.local:8123",
            token="test-token",
        )
        assert client.ws_url == "wss://homeassistant.local:8123/api/websocket"

    def test_supervisor_proxy_url_produces_core_websocket(self):
        """Supervisor proxy URL should produce ws://supervisor/core/websocket.

        This is critical for add-on WebSocket connections. The Supervisor
        proxies WebSocket connections to Home Assistant at /core/websocket,
        not at /api/websocket.

        Fixes: https://github.com/homeassistant-ai/ha-mcp/issues/186
        Fixes: https://github.com/homeassistant-ai/ha-mcp/issues/189
        """
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://supervisor/core",
            token="test-supervisor-token",
        )
        assert client.ws_url == "ws://supervisor/core/websocket"

    def test_url_with_trailing_slash_is_handled(self):
        """URL with trailing slash should work correctly."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123/",
            token="test-token",
        )
        assert client.ws_url == "ws://homeassistant.local:8123/api/websocket"

    def test_supervisor_url_with_trailing_slash_is_handled(self):
        """Supervisor URL with trailing slash should work correctly."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://supervisor/core/",
            token="test-supervisor-token",
        )
        assert client.ws_url == "ws://supervisor/core/websocket"

    def test_custom_path_url_uses_path_plus_websocket(self):
        """URL with custom path should append /websocket to the path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://proxy.local/homeassistant",
            token="test-token",
        )
        assert client.ws_url == "ws://proxy.local/homeassistant/websocket"

    def test_localhost_url_produces_standard_websocket_path(self):
        """Localhost URL should use standard /api/websocket path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://localhost:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://localhost:8123/api/websocket"

    def test_ip_address_url_produces_standard_websocket_path(self):
        """IP address URL should use standard /api/websocket path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://192.168.1.100:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://192.168.1.100:8123/api/websocket"

    def test_base_url_is_stored_without_trailing_slash(self):
        """Base URL should be stored without trailing slash."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123/",
            token="test-token",
        )
        assert client.base_url == "http://homeassistant.local:8123"

    def test_token_is_stored(self):
        """Token should be stored for authentication."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="my-secret-token",
        )
        assert client.token == "my-secret-token"


class TestSendCommandErrorContract:
    """Tests that pin the HomeAssistantCommandError raise contract.

    ``WebSocketClient.send_command`` and ``send_command_with_event`` raise
    ``HomeAssistantCommandError(f"Command failed: {msg}")`` when Home
    Assistant replies with ``{type: "result", success: False}``. The
    message is derived from the response's ``error`` field — dict
    payloads use ``error["message"]``, string/other payloads use
    ``str(error)``. These tests cover the raise sites at
    ``websocket_client.py`` L443 (send_command) and L524
    (send_command_with_event), which are not exercised by the
    classifier tests (those mock HomeAssistantCommandError directly).

    Mock strategy: stub ``send_json_message`` so that it resolves the
    pending-response future with a pre-built failure payload using the
    message ID carried in the outgoing message. This avoids depending
    on the private message-ID counter and keeps the tests robust to
    internal state changes.
    """

    @staticmethod
    def _prepare_client():
        """Build a client whose state passes is_ready and skips real I/O."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        client._state.mark_connected()
        client._state.mark_authenticated()
        return client

    @pytest.mark.asyncio
    async def test_send_command_raises_on_dict_error(self):
        """send_command raises HomeAssistantCommandError with dict error payload."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "unknown_error",
                        "message": "entity not available",
                    },
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command("test/ping")
        assert "Command failed:" in str(exc_info.value)
        assert "entity not available" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_command_raises_on_string_error(self):
        """send_command raises HomeAssistantCommandError when error is a string."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": "bare string error",
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command("test/ping")
        assert "Command failed:" in str(exc_info.value)
        assert "bare string error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_command_with_event_raises_on_dict_error(self):
        """send_command_with_event raises HomeAssistantCommandError on failure result."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "unknown_error",
                        "message": "system_health failure",
                    },
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command_with_event("system_health/info")
        assert "Command failed:" in str(exc_info.value)
        assert "system_health failure" in str(exc_info.value)


class TestSubscribeEventsContract:
    """Tests that pin the subscribe_events HA-wire-contract semantics.

    HA's ``handle_subscribe_events`` (websocket_api/commands.py) ends with
    ``connection.send_result(msg["id"])``; ``send_result(msg_id, result=None)``
    emits ``{"id": N, "type": "result", "success": true, "result": null}``.
    The subscription identifier is the request ``id`` — NOT a field inside
    the ``result`` payload. Previously the code looked for
    ``result["subscription"]``, which never exists, so every call raised
    ``"Failed to get subscription ID"``. The ``WebSocketListenerService``
    then left ``_listener_started = False``, every device-control call
    re-retried (and re-failed), and ``OperationManager.process_state_change``
    was never invoked — leaving every async operation in PENDING until
    the per-operation timeout flipped it to TIMEOUT. Surfaced during
    PR #1375 HAOS log audit.
    """

    @staticmethod
    def _prepare_client():
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        client._state.mark_connected()
        client._state.mark_authenticated()
        return client

    @pytest.mark.asyncio
    async def test_returns_message_id_with_null_result(self):
        """HA's canonical ``result: null`` reply must NOT raise.

        Pins the wire contract: ``subscribe_events`` returns the message
        ``id`` from the original command, regardless of what HA puts in
        ``result`` (HA always sends ``null`` for this command).
        """
        client = self._prepare_client()
        captured_id: dict[str, int] = {}

        async def _resolve(message: dict) -> None:
            message_id = message["id"]
            captured_id["id"] = message_id
            future = client._state._pending_requests.get(message_id)
            assert future is not None
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        subscription_id = await client.subscribe_events("state_changed")

        assert subscription_id == captured_id["id"], (
            f"Expected subscription_id to equal the message_id used in the "
            f"subscribe command, got subscription_id={subscription_id!r} "
            f"vs message_id={captured_id['id']!r}"
        )

    @pytest.mark.asyncio
    async def test_includes_event_type_in_message(self):
        """The outgoing message must carry ``event_type`` when provided."""
        client = self._prepare_client()
        captured_message: dict[str, dict] = {}

        async def _resolve(message: dict) -> None:
            captured_message["msg"] = message
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        await client.subscribe_events("state_changed")

        assert captured_message["msg"]["type"] == "subscribe_events"
        assert captured_message["msg"]["event_type"] == "state_changed"

    @pytest.mark.asyncio
    async def test_omits_event_type_when_none(self):
        """No ``event_type`` field when called with None (subscribe to all)."""
        client = self._prepare_client()
        captured_message: dict[str, dict] = {}

        async def _resolve(message: dict) -> None:
            captured_message["msg"] = message
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        await client.subscribe_events()

        assert captured_message["msg"]["type"] == "subscribe_events"
        assert "event_type" not in captured_message["msg"]

    @pytest.mark.asyncio
    async def test_raises_on_failure_result(self):
        """HA's ``{"success": false, "error": {...}}`` must surface as an error."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve(message: dict) -> None:
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "Refused to subscribe",
                    },
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.subscribe_events("state_changed")
        assert "subscribe_events failed" in str(exc_info.value)
        assert "Refused to subscribe" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_when_not_authenticated(self):
        """Subscribing before auth completes surfaces a connection error."""
        from ha_mcp.client.rest_client import HomeAssistantConnectionError
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        # Fresh client, NOT marked authenticated
        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )

        with pytest.raises(HomeAssistantConnectionError):
            await client.subscribe_events("state_changed")

    @pytest.mark.asyncio
    async def test_timeout_cancels_pending_future_and_raises(self, monkeypatch):
        """If HA never sends the subscribe-result, the 30s ``wait_for``
        deadline fires, the pending future is cancelled (preventing a
        leaked state entry), and ``TimeoutError`` propagates.

        Pins the third raise site in subscribe_events. Without this, a
        regression that swallows the timeout or forgets the cleanup
        would only surface as a stuck WS subscription dict on the
        production server.
        """
        client = self._prepare_client()

        # Drop the message on the floor — never resolve the future.
        async def _drop(message: dict) -> None:
            return None

        client.send_json_message = _drop  # type: ignore[method-assign]

        # Shorten the deadline so the test doesn't actually wait 30s.
        # Patches asyncio.wait_for to immediately raise TimeoutError.
        async def _instant_timeout(_coro, timeout):
            raise TimeoutError("test-injected timeout")

        monkeypatch.setattr(
            "ha_mcp.client.websocket_client.asyncio.wait_for", _instant_timeout
        )

        with pytest.raises(TimeoutError):
            await client.subscribe_events("state_changed")

        # Pending future for the subscribe message must have been cleaned up.
        assert not client._state._pending_requests, (
            f"Expected pending_requests to be cleaned up after timeout, "
            f"still have: {list(client._state._pending_requests)}"
        )


class TestSubscribeCommand:
    """``subscribe_command``: generic subscribe path used for HACS' ``hacs/subscribe``.

    Distinct from ``subscribe_events`` in two ways:
    1. Arbitrary command type and kwargs (not just ``subscribe_events``).
    2. Returns a queue that receives EVERY subsequent event with the
       subscription id, not just the first one (the one-shot
       ``_event_responses`` future can't model HACS' continuous stream).
    """

    @staticmethod
    def _prepare_client():
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        client._state.mark_connected()
        client._state.mark_authenticated()
        return client

    @pytest.mark.asyncio
    async def test_returns_message_id_and_queue_on_success(self):
        """HACS-shape result reply: ``subscribe_command`` returns (id, queue)."""
        client = self._prepare_client()
        captured: dict[str, dict] = {}

        async def _resolve(message: dict) -> None:
            captured["msg"] = message
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        sub_id, queue = await client.subscribe_command(
            "hacs/subscribe", signal="hacs_dispatch_repository"
        )

        assert sub_id == captured["msg"]["id"]
        assert captured["msg"]["type"] == "hacs/subscribe"
        assert captured["msg"]["signal"] == "hacs_dispatch_repository"
        # Queue must be registered AND empty initially.
        assert client._state.get_subscription_queue(sub_id) is queue
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_failure_unregisters_queue(self):
        """Failed result must clean up — no orphan queue, no orphan future."""
        client = self._prepare_client()

        async def _resolve(message: dict) -> None:
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": "nope"},
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        from ha_mcp.client.websocket_client import HomeAssistantCommandError

        with pytest.raises(HomeAssistantCommandError):
            await client.subscribe_command("nope/subscribe")

        assert not client._state._subscription_queues
        assert not client._state._pending_requests

    @pytest.mark.asyncio
    async def test_events_for_subscription_routed_to_queue(self):
        """``_handle_event_message`` must push events into the queue, not drop them."""
        client = self._prepare_client()

        async def _resolve(message: dict) -> None:
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve  # type: ignore[method-assign]

        sub_id, queue = await client.subscribe_command(
            "hacs/subscribe", signal="hacs_dispatch_repository"
        )

        # Simulate HACS pushing TWO events on the same subscription id.
        await client._handle_event_message(
            {"id": sub_id, "type": "event", "event": {"action": "registration"}},
            sub_id,
        )
        await client._handle_event_message(
            {"id": sub_id, "type": "event", "event": {"action": "install"}},
            sub_id,
        )

        # Both events must be in the queue — the one-shot
        # ``_event_responses`` future MUST NOT have captured the first one.
        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        second = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert first["event"]["action"] == "registration"
        assert second["event"]["action"] == "install"

    @pytest.mark.asyncio
    async def test_unsubscribe_command_drops_queue_and_sends_unsubscribe(self):
        """Cleanup tears down the queue and tells HA to release the subscription."""
        client = self._prepare_client()

        async def _resolve_subscribe(message: dict) -> None:
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _resolve_subscribe  # type: ignore[method-assign]
        sub_id, queue = await client.subscribe_command(
            "hacs/subscribe", signal="hacs_dispatch_repository"
        )
        assert client._state.get_subscription_queue(sub_id) is queue

        # Track the unsubscribe command issued during teardown.
        sent: list[dict] = []

        async def _capture_unsub(message: dict) -> None:
            sent.append(message)
            future = client._state._pending_requests.get(message["id"])
            assert future is not None
            future.set_result(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )

        client.send_json_message = _capture_unsub  # type: ignore[method-assign]

        await client.unsubscribe_command(sub_id)

        assert client._state.get_subscription_queue(sub_id) is None
        # Default unsubscribe command targets HA's standard endpoint —
        # HACS' ``hacs/subscribe`` registers into
        # ``connection.subscriptions`` so the standard release works.
        assert any(
            m["type"] == "unsubscribe_events" and m["subscription"] == sub_id
            for m in sent
        )
