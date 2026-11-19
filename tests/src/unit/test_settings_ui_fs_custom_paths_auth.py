"""The settings editor reports authentication refusals from real Core services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import DEFAULT, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import SERVICE_SET_ALLOWED_PATHS
from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import OAUTH_MODE_TOKEN

from ._component_filesystem import CALLER_TOKEN_STORE
from ._core_setup import set_up_tools_entry
from ._receipt_faults import revoke
from ._settings_ui_app import app_server, settings_http, with_env
from ._settings_ui_app import settings_world as settings_world

PATH = "/api/settings/fs-custom-paths"


@pytest.fixture(autouse=True)
def file_tools(monkeypatch: pytest.MonkeyPatch, settings_world: Any) -> None:
    with_env(
        monkeypatch, ENABLE_BETA_FEATURES="true", HAMCP_ENABLE_FILESYSTEM_TOOLS="true"
    )


@pytest.fixture(params=["read-only", "invalid", "revoked"])
async def refused(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
) -> AsyncIterator[tuple[HomeAssistantClient, str]]:
    await set_up_tools_entry(hass)
    if request.param == "read-only":
        yield read_only_client, "admin-only"
        return
    if request.param == "revoked":
        revoke(hass, core_client.token)
        yield core_client, "token"
        return
    invalid = HomeAssistantClient(
        base_url=core_client.base_url, token="invalid", verify_ssl=False
    )
    try:
        yield invalid, "token"
    finally:
        await invalid.close()


@pytest.mark.parametrize("method", ["get", "post"])
async def test_the_editor_names_cores_refusal(
    refused: tuple[HomeAssistantClient, str],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    client, expected = refused
    with_env(monkeypatch, HOMEASSISTANT_URL=client.base_url, HOMEASSISTANT_TOKEN=client.token)
    async with settings_http(app_server(monkeypatch, client)) as http:
        if method == "get":
            response = await http.get(PATH)
            assert response.status_code == 200
            assert response.json()["available"] is False
            reason = response.json()["reason"]
        else:
            response = await http.post(PATH, json={"paths": ["pyscript"]})
            assert response.status_code == 400
            reason = response.json()["error"]["message"]
    assert expected in reason.lower()
    assert "Could not reach" not in reason


async def test_oauth_mode_is_not_told_to_fix_the_server_token(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_env(monkeypatch, HOMEASSISTANT_TOKEN=OAUTH_MODE_TOKEN)
    # The settings route has no MCP request context for the OAuth client.
    proxy: Any = OAuthProxyClient(core_client.base_url)
    async with settings_http(app_server(monkeypatch, proxy)) as http:
        response = await http.get(PATH)
    assert response.json()["available"] is False
    assert "HOMEASSISTANT_TOKEN" not in response.json()["reason"]


async def test_a_second_refusal_reaches_the_editor(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_up_tools_entry(hass)
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    attempts = 0

    async def rotate_before_save(
        domain: str, service: str, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal attempts
        if domain == DOMAIN and service == SERVICE_SET_ALLOWED_PATHS:
            attempts += 1
            assert await hass.config_entries.async_unload(entry.entry_id)
            hass_storage.pop(CALLER_TOKEN_STORE)
            assert await hass.config_entries.async_setup(entry.entry_id)
        return DEFAULT

    async with settings_http(app_server(monkeypatch, core_client)) as http:
        with patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as calls:
            calls.side_effect = rotate_before_save
            response = await http.post(PATH, json={"paths": ["pyscript"]})
    assert response.status_code == 400
    assert attempts == 2
    assert "caller token" in response.json()["error"]["message"].lower()
