"""Unit tests for REST client automation-related methods.

These tests verify error handling for automation configuration operations,
especially the 405 Method Not Allowed error for addon proxy limitations.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)


class TestDeleteAutomationConfig:
    """Tests for delete_automation_config error handling."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_delete_automation_success(self, mock_client):
        """Successful automation deletion should return success response."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")

        result = await mock_client.delete_automation_config("automation.test_automation")

        assert result["identifier"] == "automation.test_automation"
        assert result["unique_id"] == "test_unique_id"
        assert result["operation"] == "deleted"
        mock_client._request.assert_called_once_with(
            "DELETE", "/config/automation/config/test_unique_id"
        )

    @pytest.mark.asyncio
    async def test_delete_automation_not_found_404(self, mock_client):
        """404 error should raise HomeAssistantAPIError with 'not found' message."""
        mock_client._resolve_automation_id = AsyncMock(return_value="nonexistent_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not found",
                status_code=404,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.nonexistent")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_automation_405_addon_proxy_limitation(self, mock_client):
        """405 error should raise HomeAssistantAPIError with helpful message.

        This tests the fix for issue #414 where automations cannot be deleted
        via the API when running ha-mcp as a Home Assistant add-on because
        the Supervisor ingress proxy blocks DELETE HTTP method.
        """
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 405 - Method Not Allowed",
                status_code=405,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        error = exc_info.value
        assert error.status_code == 405

        # Verify the error message is helpful
        error_message = str(error)
        assert "cannot delete" in error_message.lower()

        # Verify it mentions the addon proxy limitation
        assert "add-on" in error_message.lower()
        assert "supervisor" in error_message.lower()
        assert "delete" in error_message.lower()

        # Verify it provides workarounds
        assert "workaround" in error_message.lower()
        assert "pip" in error_message.lower() or "docker" in error_message.lower()
        assert "delete_" in error_message.lower()  # Prefix suggestion
        assert "home assistant ui" in error_message.lower()

    @pytest.mark.asyncio
    async def test_delete_automation_other_error_propagates(self, mock_client):
        """Other API errors should propagate unchanged."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error",
                status_code=500,
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_automation_generic_exception_propagates(self, mock_client):
        """Non-API exceptions should propagate."""
        mock_client._resolve_automation_id = AsyncMock(return_value="test_unique_id")
        mock_client._request = AsyncMock(
            side_effect=RuntimeError("Unexpected error")
        )

        with pytest.raises(RuntimeError) as exc_info:
            await mock_client.delete_automation_config("automation.test_automation")

        assert "Unexpected error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_automation_with_unique_id_directly(self, mock_client):
        """Should work with unique_id passed directly (not entity_id)."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._resolve_automation_id = AsyncMock(return_value="direct_unique_id")

        result = await mock_client.delete_automation_config("direct_unique_id")

        assert result["identifier"] == "direct_unique_id"
        assert result["unique_id"] == "direct_unique_id"
        assert result["operation"] == "deleted"


class TestUpsertAutomationConfigIdMismatch:
    """Guard against silent overwrite when ``identifier`` and ``config['id']``
    disagree (#1404).

    Home Assistant's automation storage uses the inner ``config['id']`` as the
    primary key. If the MCP server POSTs to ``/config/automation/config/{X}``
    with a body carrying ``id=Y``, HA stores the body under ``Y`` and the
    automation that previously owned ``Y`` is silently overwritten — while the
    response reports success for ``X``. The guard converts that silent data
    loss into a structured 400 error.
    """

    @pytest.fixture
    def mock_client(self):
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    @pytest.mark.asyncio
    async def test_update_with_mismatched_inner_id_is_rejected(self, mock_client):
        """identifier resolves to AAA but config carries id=BBB → reject."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "BBB", "alias": "x", "trigger": [], "action": []},
                identifier="automation.foo",
            )

        assert exc_info.value.status_code == 400
        msg = str(exc_info.value)
        assert "Mismatched" in msg
        assert "'AAA'" in msg
        assert "'BBB'" in msg
        # Critical: the offending POST never reaches HA.
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_with_matching_inner_id_proceeds(self, mock_client):
        """identifier and config.id both resolve to the same unique_id → ok."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(return_value=None)

        result = await mock_client.upsert_automation_config(
            {"id": "AAA", "alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
        )

        assert result["unique_id"] == "AAA"
        assert result["operation"] == "updated"
        mock_client._request.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_without_inner_id_proceeds(self, mock_client):
        """No inner id → existing code path injects unique_id, no guard fires."""
        mock_client._resolve_automation_id = AsyncMock(return_value="AAA")
        mock_client._request = AsyncMock(return_value={"result": "ok"})

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier="automation.foo",
        )

        assert result["unique_id"] == "AAA"
        # Sent body should carry the resolved id.
        sent_config = mock_client._request.call_args.kwargs["json"]
        assert sent_config["id"] == "AAA"

    @pytest.mark.asyncio
    async def test_create_with_inner_id_is_rejected(self, mock_client):
        """identifier=None + config['id']=X → reject (would silently overwrite
        X if it exists; would create with caller-chosen id if it doesn't —
        both are agent-slip patterns the guard converts to a loud error)."""
        mock_client._request = AsyncMock()

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.upsert_automation_config(
                {"id": "BBB", "alias": "x", "trigger": [], "action": []},
                identifier=None,
            )

        assert exc_info.value.status_code == 400
        msg = str(exc_info.value)
        assert "Cannot create" in msg
        assert "'BBB'" in msg
        assert "Omit 'id'" in msg
        mock_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_without_inner_id_proceeds(self, mock_client):
        """Normal create path: no inner id, fresh timestamp assigned."""
        mock_client._request = AsyncMock(return_value={"result": "ok"})
        mock_client._poll_for_automation_entity = AsyncMock(
            return_value="automation.new"
        )

        result = await mock_client.upsert_automation_config(
            {"alias": "x", "trigger": [], "action": []},
            identifier=None,
        )

        assert result["operation"] == "created"
        assert result["entity_id"] == "automation.new"


class TestPollForAutomationEntity:
    """Tests for the poll cadence in _poll_for_automation_entity (issue #1380).

    Patch-path note: tests patch ``ha_mcp.client.rest_client.asyncio.sleep``
    by attribute access. A refactor to ``from asyncio import sleep`` in the
    production module would silently let real sleeps run — the full-miss
    test would hang for ~6s instead of completing instantly. If that
    refactor lands, update the patch target to
    ``ha_mcp.client.rest_client.sleep``.
    """

    @pytest.fixture
    def mock_client(self):
        """Create a mock HomeAssistantClient for testing."""
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            return client

    def test_poll_cadence_shape(self):
        """Pin the cadence tuple plus the sub-200ms first-poll bound — length
        and sum are derivable from the tuple and would fail redundantly on a
        mutation."""
        assert HomeAssistantClient._POLL_CADENCE == (0.1, 1.0, 4.9)
        assert HomeAssistantClient._POLL_CADENCE[0] <= 0.2

    @pytest.mark.asyncio
    async def test_poll_returns_entity_id_on_first_attempt(self, mock_client):
        """When HA publishes the entity within the first 0.1s window, the
        poll returns on iteration 1 and only sleeps once."""
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.test_target",
                    "attributes": {"id": "unique_42"},
                }
            ]
        )
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result == "automation.test_target"
        assert sleep_calls == [0.1]
        assert mock_client.get_states.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_returns_none_on_full_miss(self, mock_client):
        """When the entity never appears, the poll exhausts the cadence,
        sleeps for the full 6s budget across 3 attempts, and returns None."""
        mock_client.get_states = AsyncMock(return_value=[])
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None
        assert sleep_calls == [0.1, 1.0, 4.9]
        assert mock_client.get_states.call_count == 3

    @pytest.mark.asyncio
    async def test_poll_returns_entity_id_on_later_attempt(self, mock_client):
        """When HA publishes after the first poll, later iterations succeed
        without sleeping for the full cadence."""
        # First call returns no match, second call returns the match.
        mock_client.get_states = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "entity_id": "automation.slow_target",
                        "attributes": {"id": "unique_99"},
                    }
                ],
            ]
        )
        sleep_calls: list[float] = []

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_99")

        assert result == "automation.slow_target"
        assert sleep_calls == [0.1, 1.0]
        assert mock_client.get_states.call_count == 2

    @pytest.mark.asyncio
    async def test_poll_ignores_non_automation_entities(self, mock_client):
        """States not starting with `automation.` are skipped so we don't
        match a script/scene that happens to carry the same id attribute."""
        mock_client.get_states = AsyncMock(
            return_value=[
                {"entity_id": "script.distractor", "attributes": {"id": "unique_42"}},
                {
                    "entity_id": "automation.actual",
                    "attributes": {"id": "unique_42"},
                },
            ]
        )

        async def fake_sleep(duration: float) -> None:
            return None

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result == "automation.actual"

    @pytest.mark.asyncio
    async def test_poll_swallows_get_states_exception(self, mock_client):
        """Transient ``get_states`` failures yield None (and
        ``entity_not_verified=True`` upstream), not a propagated exception.
        Uses ``HomeAssistantAPIError`` — the realistic transient class,
        mirrors ``_POLLING_TRANSIENT_ERRORS`` in ``wait_helpers.py``.
        ``call_count == 1`` locks the wrap-scope: the ``try`` covers the
        entire ``for``, so iteration-1 transients exit immediately."""
        mock_client.get_states = AsyncMock(
            side_effect=HomeAssistantAPIError("transient")
        )

        async def fake_sleep(duration: float) -> None:
            return None

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None
        assert mock_client.get_states.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_swallows_get_states_exception_on_iteration_2(
        self, mock_client
    ):
        """Mid-loop transient also exits early — locks the wrap-scope: the
        ``try`` covers the entire ``for`` (not the loop body), so a transient
        on iteration 2 hits the ``except HomeAssistantError`` clause and
        returns immediately. ``call_count == 2`` would fail if a future
        refactor moved the ``try`` inside the loop (which would retry
        transients until cadence exhaustion)."""
        mock_client.get_states = AsyncMock(
            side_effect=[[], HomeAssistantAPIError("transient on iteration 2")]
        )

        async def fake_sleep(duration: float) -> None:
            return None

        with patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep):
            result = await mock_client._poll_for_automation_entity("unique_42")

        assert result is None
        assert mock_client.get_states.call_count == 2

    @pytest.mark.asyncio
    async def test_poll_propagates_typeerror_for_unexpected_errors(
        self, mock_client
    ):
        """Programming bugs (TypeError, KeyError, AttributeError, …) must
        propagate — they are not transient, and swallowing them would mask
        the bug as ``entity_not_verified=True``. Locks the narrowed
        ``except HomeAssistantError`` clause: a future widen-back to
        ``except Exception`` would fail this test."""
        mock_client.get_states = AsyncMock(side_effect=TypeError("bug"))

        async def fake_sleep(duration: float) -> None:
            return None

        with (
            patch("ha_mcp.client.rest_client.asyncio.sleep", new=fake_sleep),
            pytest.raises(TypeError, match="bug"),
        ):
            await mock_client._poll_for_automation_entity("unique_42")
