"""Standalone OAuth must issue each authorization code the token consented for it."""

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
from ha_mcp.config import OAUTH_MODE_TOKEN

from ...conftest import TEST_TOKEN
from ...utilities.assertions import MCPAssertions
from ..hacs.test_auto_refresh_startup import _http_launcher_env, _spawn_http_launcher

logger = logging.getLogger(__name__)
pytestmark = [pytest.mark.container_only]

REDIRECT_URI = "http://localhost/callback"


async def _register_public_client(http: httpx.AsyncClient) -> str:
    registered = await http.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Consent token binding",
        },
    )
    assert registered.status_code == 201, registered.text
    return registered.json()["client_id"]


async def _consent(
    http: httpx.AsyncClient, client_id: str, ha_token: str
) -> tuple[str, str]:
    """Run authorize + consent for one user; return the code and its verifier."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    authorize = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "binding",
            "scope": "homeassistant",
        },
    )
    assert authorize.status_code == 302, authorize.text
    txn_id = parse_qs(urlparse(authorize.headers["location"]).query)["txn_id"][0]
    consent = await http.post("/consent", data={"txn_id": txn_id, "ha_token": ha_token})
    assert consent.status_code == 303, consent.text
    code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
    return code, verifier


async def _exchange(
    http: httpx.AsyncClient, client_id: str, code: str, verifier: str
) -> httpx.Response:
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


# Launcher startup, two OAuth flows, one MCP call, teardown.
@pytest.mark.timeout(240)
async def test_overlapping_consents_each_redeem_their_own_token(
    ha_container_with_fresh_config: dict,
    tmp_path: Path,
    unused_tcp_port: int,
) -> None:
    """Two users consenting through one client must each get their own token.

    The first user's token used to be replaced by whichever consent came
    later for the same client, so the first user ended up acting with the
    second user's credential and the second user's exchange failed.
    """
    container = ha_container_with_fresh_config
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
            client_id = await _register_public_client(http)
            first_code, first_verifier = await _consent(
                http, client_id, container.get("token", TEST_TOKEN)
            )
            second_code, second_verifier = await _consent(
                http, client_id, "not-a-home-assistant-token"
            )
            first = await _exchange(http, client_id, first_code, first_verifier)
            assert first.status_code == 200, first.text
            second = await _exchange(http, client_id, second_code, second_verifier)
            assert second.status_code == 200, second.text

        # The first user's access token must reach Home Assistant with the
        # first user's credential, which is the only valid one of the two.
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
