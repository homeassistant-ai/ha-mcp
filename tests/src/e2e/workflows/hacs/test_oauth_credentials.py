"""Standalone OAuth must use the consenting user's HA token for HACS (#2687)."""

import logging
from pathlib import Path

import httpx
import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport
from ha_mcp.client.rest_client import HomeAssistantAuthError, HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import OAUTH_MODE_TOKEN

from ...conftest import TEST_TOKEN
from ...utilities.assertions import MCPAssertions
from ...utilities.http_launcher import http_launcher_env, spawn_http_launcher
from ...utilities.oauth_flow import (
    consent,
    exchange,
    register_public_client,
    wait_for_discovery,
)
from .test_auto_refresh_startup import HACS_WS_READY_TIMEOUT, _wait_for_hacs_ws_ready

logger = logging.getLogger(__name__)
pytestmark = [pytest.mark.container_only, pytest.mark.hacs]


REDIRECT_URI = "http://localhost/callback"


async def _authorize(http: httpx.AsyncClient, ha_token: str) -> str:
    """Complete public-client PKCE registration and consent over real HTTP."""
    await wait_for_discovery(http)
    client_id = await register_public_client(
        http, REDIRECT_URI, client_name="HACS OAuth regression"
    )
    code, verifier = await consent(
        http, client_id, REDIRECT_URI, ha_token, state="hacs-regression"
    )
    issued = await exchange(http, client_id, REDIRECT_URI, code, verifier)
    assert issued.status_code == 200, issued.text
    return issued.json()["access_token"]


# Allow launcher startup, OAuth requests, MCP calls, and teardown after HACS is ready.
@pytest.mark.timeout(HACS_WS_READY_TIMEOUT + 180)
async def test_hacs_uses_admin_oauth_session_instead_of_global_placeholder(
    ha_container_with_fresh_config: dict,
    ha_client: HomeAssistantClient,
    tmp_path: Path,
    unused_tcp_port: int,
) -> None:
    """A real admin login must reach HACS as well as dashboards through OAuth."""
    container = ha_container_with_fresh_config
    await _wait_for_hacs_ws_ready(container)
    identity = await ha_client.send_websocket_message({"type": "auth/current_user"})
    assert identity["result"]["is_admin"] is True
    base_url = f"http://127.0.0.1:{unused_tcp_port}"
    env = http_launcher_env(container["base_url"], tmp_path)
    env.update(
        HOMEASSISTANT_TOKEN=OAUTH_MODE_TOKEN,
        MCP_BASE_URL=base_url,
        MCP_PORT=str(unused_tcp_port),
        HA_MCP_DISABLE_UPDATE_CHECK="true",
        HA_MCP_DISABLE_SETTINGS_UI="true",
    )
    launcher = await spawn_http_launcher("ha-mcp-oauth", env)
    try:
        assert await launcher.wait_for_lifespan_started(), launcher.output()
        async with httpx.AsyncClient(base_url=base_url) as http:
            token = await _authorize(http, container.get("token", TEST_TOKEN))
        transport = StreamableHttpTransport(f"{base_url}/e2e-nudge-probe", auth=token)
        async with (
            Client(transport, timeout=60) as client,
            MCPAssertions(client) as mcp,
        ):
            await mcp.call_tool_success(
                "ha_get_hacs_info",
                {"action": "search", "installed_only": True},
            )
            await mcp.call_tool_success("ha_config_get_dashboard", {"list_only": True})
    finally:
        logger.info("OAuth launcher output:\n%s", launcher.output())
        await launcher.aclose()


async def test_rejected_websocket_token_surfaces_auth_error_not_timeout(
    ha_container_with_fresh_config: dict,
) -> None:
    """HA's auth_invalid followed by close must retain its failure classification."""
    client = HomeAssistantWebSocketClient(
        ha_container_with_fresh_config["base_url"], OAUTH_MODE_TOKEN
    )
    try:
        assert await client.connect() is False
        assert isinstance(client.last_connect_exception, HomeAssistantAuthError), (
            client.last_connect_error
        )
        client.token = ha_container_with_fresh_config.get("token", TEST_TOKEN)
        assert await client.connect() is True
        assert client.last_connect_exception is None
        user = await client.send_command("auth/current_user")
        assert user["result"]["is_admin"] is True
    finally:
        await client.disconnect()
