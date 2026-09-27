"""Non-admin token guard against a real Home Assistant (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with 401, which ``http.ban`` counts as a failed login. The REST client asks
``auth/current_user`` and refuses those routes locally instead of sending them.
The non-admin user and its token are seeded (``tests/test_constants.py``).
"""

from collections.abc import AsyncGenerator

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantClient,
)
from tests.test_constants import NON_ADMIN_TEST_TOKEN

_ADMIN_ONLY_PATH = "/config/automation/config/ha_mcp_e2e_absent"


@pytest.fixture
async def non_admin_client(
    ha_client: HomeAssistantClient,
) -> AsyncGenerator[HomeAssistantClient]:
    client = HomeAssistantClient(
        base_url=ha_client.base_url,
        token=NON_ADMIN_TEST_TOKEN,
        verify_ssl=ha_client.verify_ssl,
    )
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_probe_reads_admin_status_from_home_assistant(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    assert await ha_client.token_is_admin() is True
    assert await non_admin_client.token_is_admin() is False


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
    ha_client: HomeAssistantClient,
):
    """The premise: the route is admin-only and the refusal is a 401."""
    try:
        async with httpx.AsyncClient(verify=ha_client.verify_ssl) as http:
            response = await http.get(
                f"{ha_client.base_url}/api{_ADMIN_ONLY_PATH}",
                headers={"Authorization": f"Bearer {NON_ADMIN_TEST_TOKEN}"},
            )
        assert response.status_code == 401
    finally:
        # http.ban posts a "Login attempt failed" notification for the 401.
        await ha_client.call_service(
            "persistent_notification", "dismiss", {"notification_id": "http-login"}
        )
