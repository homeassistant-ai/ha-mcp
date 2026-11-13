"""The private LLM-API metadata reaches only the catalog that consumes it (#2479).

Only the custom component's conversation-agent LLM API reads the per-tool
``_meta.ha_mcp`` stamp. The real server is built for each deployment shape and
its ``tools/list`` is read over an MCP client session.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ha_mcp import config
from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.llm_exposure import META_NAMESPACE

from ._real_server import build_server


@pytest.fixture(autouse=True)
def standalone() -> Iterator[None]:
    """Start each case as a standalone server, and leave no registration behind.

    The embedded registration is process-global and only the component's
    in-process entry sets it (``set_embedded_connection``), so it is cleared
    before and after each case.
    """
    config.reset_embedded_connection()
    yield
    config.reset_embedded_connection()


async def _stamped_tools(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> tuple[int, int]:
    """(listed tools, tools carrying the private stamp) as an MCP client sees them."""
    server = build_server(monkeypatch, client)
    async with Client(server.mcp) as session:
        tools = await session.list_tools()
    stamped = [tool for tool in tools if META_NAMESPACE in (tool.meta or {})]
    return len(tools), len(stamped)


@pytest.mark.parametrize("embedded_llm_api_enabled", [None, False])
async def test_private_metadata_is_absent_without_an_llm_api_consumer(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    embedded_llm_api_enabled: bool | None,
) -> None:
    """Standalone and LLM-API-disabled embedded catalogs stay unmodified."""
    if embedded_llm_api_enabled is not None:
        config.set_embedded_connection(
            "http://127.0.0.1:8123",
            "test-token",
            llm_api_enabled=embedded_llm_api_enabled,
        )

    listed, stamped = await _stamped_tools(monkeypatch, core_client)

    assert listed
    assert stamped == 0


async def test_private_metadata_is_present_for_enabled_embedded_llm_api(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The component's enabled conversation-agent API gets every tool stamped."""
    config.set_embedded_connection(
        "http://127.0.0.1:8123", "test-token", llm_api_enabled=True
    )

    listed, stamped = await _stamped_tools(monkeypatch, core_client)

    assert listed
    assert stamped == listed
