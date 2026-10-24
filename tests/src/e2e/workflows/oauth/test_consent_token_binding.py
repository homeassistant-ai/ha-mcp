"""Standalone OAuth must issue each authorization code the token consented for it."""

import logging
from pathlib import Path

import httpx
import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.client.transports import StreamableHttpTransport
from ha_mcp.config import OAUTH_MODE_TOKEN

from ...utilities.assertions import MCPAssertions
from ...utilities.http_launcher import http_launcher_env, spawn_http_launcher
from ...utilities.oauth_flow import (
    consent,
    exchange,
    register_public_client,
    wait_for_discovery,
)

logger = logging.getLogger(__name__)
pytestmark = [pytest.mark.container_only]

REDIRECT_URI = "http://localhost/callback"


# Launcher startup, two OAuth flows, one MCP call, teardown.
@pytest.mark.timeout(240)
async def test_overlapping_consents_each_redeem_their_own_token(
    ha_container_with_fresh_config: dict,
    tmp_path: Path,
    unused_tcp_port: int,
) -> None:
    """Two users consenting through one client must each get their own token.

    The unit suite pins the binding inside the provider. This proves the
    wiring around it: the SDK's token handler passes the provider's own code
    object back for exchange, and the credential bound to the first user's
    code is the one that reaches Home Assistant. The second user consents
    with a bogus token, so a swap shows up as a real Home Assistant auth
    failure on the first user's tool call, not just a string mismatch.
    """
    container = ha_container_with_fresh_config
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
            await wait_for_discovery(http)
            client_id = await register_public_client(
                http, REDIRECT_URI, client_name="Consent token binding"
            )
            first_code, first_verifier = await consent(
                http, client_id, REDIRECT_URI, container["token"], state="first"
            )
            second_code, second_verifier = await consent(
                http,
                client_id,
                REDIRECT_URI,
                "not-a-home-assistant-token",
                state="second",
            )
            first = await exchange(
                http, client_id, REDIRECT_URI, first_code, first_verifier
            )
            assert first.status_code == 200, first.text
            second = await exchange(
                http, client_id, REDIRECT_URI, second_code, second_verifier
            )
            assert second.status_code == 200, second.text

        transport = StreamableHttpTransport(
            f"{base_url}{env['MCP_SECRET_PATH']}",
            auth=first.json()["access_token"],
        )
        async with (
            Client(transport, timeout=60) as client,
            MCPAssertions(client) as mcp,
        ):
            await mcp.call_tool_success("ha_config_get_dashboard", {"list_only": True})
    finally:
        logger.info("OAuth launcher output:\n%s", launcher.output())
        await launcher.aclose()
