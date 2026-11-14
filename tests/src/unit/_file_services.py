"""Set up the File & YAML Tools entry and call its services like ha-mcp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_SET_ALLOWED_PATHS,
)
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE

FileService = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def make_file_service(
    hass: HomeAssistant, allowed: str | None = None
) -> FileService:
    """Set up the tools entry and return a caller that presents its token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    async def call(service: str, data: dict[str, Any]) -> dict[str, Any]:
        response = await hass.services.async_call(
            DOMAIN, service, data, blocking=True, return_response=True
        )
        assert response is not None
        return dict(response)

    bootstrap = await call(SERVICE_GET_CALLER_TOKEN, {})
    assert bootstrap["success"] is True, bootstrap

    async def call_with_token(service: str, data: dict[str, Any]) -> dict[str, Any]:
        return await call(service, {**data, CALLER_TOKEN_FIELD: bootstrap["token"]})

    if allowed is not None:
        configured = await call_with_token(
            SERVICE_SET_ALLOWED_PATHS, {"paths": [allowed]}
        )
        assert configured["paths"] == [allowed], configured

    return call_with_token
