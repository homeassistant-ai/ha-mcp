"""Call the component's ``edit_yaml_config`` service on a real ``hass``.

The YAML-editing tests run with Core's config directory at ``tmp_path`` (the
conftest's ``hass_config_dir``) and call the service the way ha-mcp does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_EDIT_YAML_CONFIG,
    SERVICE_GET_CALLER_TOKEN,
)
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE

EditYaml = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def make_edit_yaml(hass: HomeAssistant) -> EditYaml:
    """Set the File & YAML Tools entry up and return a caller of its service.

    Core sets the entry up, which registers the service, and the caller token
    comes from the entry's ``get_caller_token`` service.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bootstrap = await hass.services.async_call(
        DOMAIN, SERVICE_GET_CALLER_TOKEN, {}, blocking=True, return_response=True
    )
    assert bootstrap is not None and bootstrap["success"] is True, bootstrap
    token = bootstrap["token"]

    async def edit(data: dict[str, Any]) -> dict[str, Any]:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_EDIT_YAML_CONFIG,
            {**data, CALLER_TOKEN_FIELD: token},
            blocking=True,
            return_response=True,
        )
        assert response is not None
        return dict(response)

    return edit
