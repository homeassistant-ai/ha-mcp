"""Standalone OAuth must use the consenting user's HA token for HACS (#2687)."""

import base64
import hashlib
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport
from ha_mcp.client.rest_client import HomeAssistantAuthError, HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import OAUTH_MODE_TOKEN

from ...conftest import TEST_TOKEN
from ...utilities.assertions import MCPAssertions
from .test_auto_refresh_startup import (
    HACS_WS_READY_TIMEOUT,
    _http_launcher_env,
    _spawn_http_launcher,
    _wait_for_hacs_ws_ready,
)

logger = logging.getLogger(__name__)
pytestmark = [pytest.mark.container_only, pytest.mark.hacs]


async def _authorize(http: httpx.AsyncClient, ha_token: str) -> str:
    """Complete public-client PKCE registration and consent over real HTTP."""
    redirect_uri = "http://localhost/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    registered = await http.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "HACS OAuth regression",
        },
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    authorize = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "hacs-regression",
            "scope": "homeassistant",
        },
    )
    assert authorize.status_code == 302
    txn_id = parse_qs(urlparse(authorize.headers["location"]).query)["txn_id"][0]
    consent = await http.post("/consent", data={"txn_id": txn_id, "ha_token": ha_token})
    assert consent.status_code == 303
    callback = parse_qs(urlparse(consent.headers["location"]).query)
    assert callback["state"] == ["hacs-regression"]
    issued = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": callback["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert issued.status_code == 200
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
    env = _http_launcher_env(container["base_url"], tmp_path)
    env.update(
        HOMEASSISTANT_TOKEN=OAUTH_MODE_TOKEN,
        MCP_BASE_URL=base_url,
        MCP_PORT=str(unused_tcp_port),
        HA_MCP_DISABLE_UPDATE_CHECK="true",
        HA_MCP_DISABLE_SETTINGS_UI="true",
    )
    launcher = await _spawn_http_launcher("ha-mcp-oauth", env)
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
