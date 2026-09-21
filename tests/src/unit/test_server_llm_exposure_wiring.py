"""Server-level coverage for the private LLM-API metadata middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ha_mcp import config
from ha_mcp.llm_exposure import LlmExposureMiddleware
from ha_mcp.server import HomeAssistantSmartMCPServer


@pytest.fixture(autouse=True)
def _reset_embedded_connection():
    """Keep the process-global embedded registration isolated per case."""
    config._reset_embedded_connection()
    yield
    config._reset_embedded_connection()


def _registered_llm_exposure_middleware(server: MagicMock) -> list[object]:
    return [
        args[0]
        for name, args, _kwargs in server.mcp.mock_calls
        if name == "add_middleware"
        and args
        and isinstance(args[0], LlmExposureMiddleware)
    ]


@pytest.mark.parametrize("embedded_llm_api_enabled", [None, False])
def test_private_metadata_middleware_is_absent_without_an_llm_api_consumer(
    embedded_llm_api_enabled: bool | None,
):
    """Standalone and LLM-API-disabled embedded catalogs stay unmodified."""
    if embedded_llm_api_enabled is not None:
        config.set_embedded_connection(
            "http://127.0.0.1:8123",
            "test-token",
            llm_api_enabled=embedded_llm_api_enabled,
        )

    server = MagicMock()
    server.mcp = MagicMock()
    server.settings.ha_tool_concurrency = 0

    HomeAssistantSmartMCPServer._initialize_server(server)

    assert not _registered_llm_exposure_middleware(server)


def test_private_metadata_middleware_is_present_for_enabled_embedded_llm_api():
    """The component's enabled conversation-agent API retains its stamp."""
    config.set_embedded_connection(
        "http://127.0.0.1:8123", "test-token", llm_api_enabled=True
    )
    server = MagicMock()
    server.mcp = MagicMock()
    server.settings.ha_tool_concurrency = 0

    HomeAssistantSmartMCPServer._initialize_server(server)

    assert len(_registered_llm_exposure_middleware(server)) == 1
