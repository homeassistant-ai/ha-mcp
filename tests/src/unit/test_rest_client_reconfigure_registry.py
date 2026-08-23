"""Tests for config-entry reconfigure registry reads."""

from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient


@pytest.fixture
def client() -> HomeAssistantClient:
    """Create a client without opening a network connection."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        value = HomeAssistantClient()
        value.base_url = "http://test.local:8123"
        value.token = "test-token"
        value.timeout = 30
        value.verify_ssl = True
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["list_entity_registry", "list_device_registry"]
)
async def test_registry_list_accepts_valid_result_envelope(
    client: HomeAssistantClient, method_name: str
) -> None:
    """Registry methods return dictionaries from a valid WebSocket result."""
    with patch.object(
        client,
        "send_websocket_message",
        new=AsyncMock(return_value={"success": True, "result": [{"id": "device-1"}]}),
    ):
        result = await getattr(client, method_name)()

    assert result == [{"id": "device-1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["list_entity_registry", "list_device_registry"]
)
async def test_registry_list_rejects_error_envelope(
    client: HomeAssistantClient, method_name: str
) -> None:
    """A WebSocket error envelope is not silently converted to an empty list."""
    with (
        patch.object(
            client,
            "send_websocket_message",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "error": {"code": "not_loaded", "message": "registry unavailable"},
                }
            ),
        ),
        pytest.raises(HomeAssistantAPIError, match="registry unavailable"),
    ):
        await getattr(client, method_name)()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["list_entity_registry", "list_device_registry"]
)
async def test_registry_list_rejects_malformed_result(
    client: HomeAssistantClient, method_name: str
) -> None:
    """A malformed result cannot disable identity and duplicate checks."""
    with (
        patch.object(
            client,
            "send_websocket_message",
            new=AsyncMock(return_value={"success": True, "result": ["not-a-row"]}),
        ),
        pytest.raises(HomeAssistantAPIError, match="Unexpected response"),
    ):
        await getattr(client, method_name)()
