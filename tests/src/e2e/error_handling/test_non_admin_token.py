"""Non-admin token guard against a real Home Assistant (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with 401, which ``http.ban`` counts as a failed login. The REST client asks
``auth/current_user`` and refuses those routes locally instead of sending them.
"""

import logging
import secrets
from collections.abc import AsyncGenerator

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantClient,
)

logger = logging.getLogger(__name__)

_ADMIN_ONLY_PATH = "/config/automation/config/ha_mcp_e2e_absent"


async def _login_access_token(
    base_url: str, verify_ssl: bool, username: str, password: str
) -> str:
    """Run Home Assistant's login flow for ``username`` and return its access token."""
    client_id = f"{base_url}/"
    async with httpx.AsyncClient(base_url=base_url, verify=verify_ssl) as http:
        flow = await http.post(
            "/auth/login_flow",
            json={
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": client_id,
            },
        )
        flow.raise_for_status()
        step = await http.post(
            f"/auth/login_flow/{flow.json()['flow_id']}",
            json={"client_id": client_id, "username": username, "password": password},
        )
        step.raise_for_status()
        token = await http.post(
            "/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": step.json()["result"],
                "client_id": client_id,
            },
        )
        token.raise_for_status()
        access_token: str = token.json()["access_token"]
        return access_token


@pytest.fixture
async def non_admin_client(
    ha_client: HomeAssistantClient,
) -> AsyncGenerator[HomeAssistantClient]:
    """A REST client authenticated as a fresh ``system-users`` (non-admin) user."""
    created = await ha_client.send_websocket_message(
        {
            "type": "config/auth/create",
            "name": "ha-mcp e2e non-admin",
            "group_ids": ["system-users"],
        }
    )
    assert created.get("success"), f"config/auth/create failed: {created}"
    user_id = created["result"]["user"]["id"]
    client: HomeAssistantClient | None = None
    try:
        username = f"hamcp-e2e-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
        provider = await ha_client.send_websocket_message(
            {
                "type": "config/auth_provider/homeassistant/create",
                "user_id": user_id,
                "username": username,
                "password": password,
            }
        )
        assert provider.get("success"), f"credential create failed: {provider}"
        token = await _login_access_token(
            ha_client.base_url, ha_client.verify_ssl, username, password
        )
        client = HomeAssistantClient(
            base_url=ha_client.base_url, token=token, verify_ssl=ha_client.verify_ssl
        )
        yield client
    finally:
        if client is not None:
            await client.close()
        await ha_client.send_websocket_message(
            {"type": "config/auth/delete", "user_id": user_id}
        )


@pytest.mark.asyncio
async def test_probe_reads_admin_status_from_home_assistant(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    assert await ha_client._token_is_admin() is True
    assert await non_admin_client._token_is_admin() is False


@pytest.mark.asyncio
async def test_non_admin_admin_only_route_is_refused_without_a_request(
    non_admin_client: HomeAssistantClient,
):
    sent: list[str] = []

    async def record(request: httpx.Request) -> None:
        sent.append(request.url.path)

    non_admin_client.httpx_client.event_hooks["request"].append(record)

    with pytest.raises(HomeAssistantAdminRequiredError):
        await non_admin_client._request("GET", _ADMIN_ONLY_PATH)
    assert sent == []

    # Routes Home Assistant opens to every user keep working.
    states = await non_admin_client.get_states()
    assert isinstance(states, list) and states
    assert sent == ["/api/states"]


@pytest.mark.asyncio
async def test_home_assistant_answers_non_admin_with_401(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    """The premise: the route is admin-only and the refusal is a 401."""
    try:
        async with httpx.AsyncClient(verify=ha_client.verify_ssl) as http:
            response = await http.get(
                f"{ha_client.base_url}/api{_ADMIN_ONLY_PATH}",
                headers={"Authorization": f"Bearer {non_admin_client.token}"},
            )
        assert response.status_code == 401
    finally:
        # http.ban posts a "Login attempt failed" notification for the 401.
        await ha_client.call_service(
            "persistent_notification", "dismiss", {"notification_id": "http-login"}
        )
