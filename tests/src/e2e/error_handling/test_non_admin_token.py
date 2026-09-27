"""Non-admin token guard against a real Home Assistant (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with 401, which ``http.ban`` counts as a failed login. The REST client asks
``auth/current_user`` and refuses those routes locally instead of sending them.
The non-admin user and its token are seeded (``tests/test_constants.py``).
"""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp.client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
    HomeAssistantClient,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from tests.test_constants import NON_ADMIN_TEST_TOKEN

from ..utilities.assertions import assert_mcp_success, safe_call_tool

_ADMIN_ONLY_PATH = "/config/automation/config/ha_mcp_e2e_absent"
# Seeded in tests/initial_test_state/automations.yaml.
_SEED_AUTOMATION_PATH = "/config/automation/config/e2e_test_automation_seed"


def _record_requests(client: HomeAssistantClient) -> list[str]:
    sent: list[str] = []

    async def record(request: httpx.Request) -> None:
        sent.append(request.url.path)

    client.httpx_client.event_hooks["request"].append(record)
    return sent


async def _dismiss_login_notification(ha_client: HomeAssistantClient) -> None:
    """http.ban posts a "Login attempt failed" notification for every 401."""
    await ha_client.call_service(
        "persistent_notification", "dismiss", {"notification_id": "http-login"}
    )


async def _diagnostics_path(ha_client: HomeAssistantClient) -> str:
    entries = await ha_client._request("GET", "/config/config_entries/entry")
    entry_id = next(e["entry_id"] for e in entries if e["domain"] == "sun")
    return f"/diagnostics/config_entry/{entry_id}"


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
    sent = _record_requests(non_admin_client)

    with pytest.raises(HomeAssistantAdminRequiredError):
        await non_admin_client._request("GET", _ADMIN_ONLY_PATH)
    assert sent == []

    # Routes Home Assistant opens to every user keep working.
    states = await non_admin_client.get_states()
    assert isinstance(states, list) and states
    assert sent == ["/api/states"]


@pytest.mark.asyncio
async def test_diagnostics_is_refused_without_a_request(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    path = await _diagnostics_path(ha_client)
    sent = _record_requests(non_admin_client)
    with pytest.raises(HomeAssistantAdminRequiredError):
        await non_admin_client._request("GET", path)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["automation_config", "diagnostics"])
async def test_home_assistant_answers_non_admin_with_401(
    ha_client: HomeAssistantClient, route: str
):
    """The premise for each listed route: core refuses a non-admin with 401."""
    path = (
        _ADMIN_ONLY_PATH
        if route == "automation_config"
        else await _diagnostics_path(ha_client)
    )
    try:
        async with httpx.AsyncClient(verify=ha_client.verify_ssl) as http:
            response = await http.get(
                f"{ha_client.base_url}/api{path}",
                headers={"Authorization": f"Bearer {NON_ADMIN_TEST_TOKEN}"},
            )
        assert response.status_code == 401
    finally:
        await _dismiss_login_notification(ha_client)


@pytest.mark.asyncio
async def test_unanswered_probe_lets_one_concurrent_request_reach_home_assistant(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    """With no ``auth/current_user`` answer, a parallel burst sends one request.

    The first 401 latches; the rest are refused locally instead of each adding
    a failed login toward ``http.ban``.
    """
    non_admin_client._current_user = AsyncMock(return_value=None)  # type: ignore[method-assign]
    sent = _record_requests(non_admin_client)
    try:
        results = await asyncio.gather(
            *(
                non_admin_client._request("GET", _SEED_AUTOMATION_PATH)
                for _ in range(10)
            ),
            return_exceptions=True,
        )
    finally:
        await _dismiss_login_notification(ha_client)
    assert all(isinstance(r, HomeAssistantAuthError) for r in results), results
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["answered", "unanswered"])
async def test_config_body_search_warns_about_the_token(
    ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient, probe: str
):
    """A refused config-body search is partial and carries the warning.

    ``unanswered`` reaches the refusal through a real 401 rather than the probe,
    so no failure sample names the guard's error type.
    """
    if probe == "unanswered":
        non_admin_client._current_user = AsyncMock(return_value=None)  # type: ignore[method-assign]
    try:
        result = await SmartSearchTools(client=non_admin_client).deep_search(
            "E2E Seed Automation", search_types=["automation"], limit=10
        )
    finally:
        await _dismiss_login_notification(ha_client)
    assert result.get("partial") is True, result
    assert NON_ADMIN_TOKEN_WARNING in result.get("warnings", []), result

    control = await SmartSearchTools(client=ha_client).deep_search(
        "E2E Seed Automation", search_types=["automation"], limit=10
    )
    assert NON_ADMIN_TOKEN_WARNING not in control.get("warnings", []), control
    assert any(
        a["entity_id"] == "automation.e2e_seed_automation"
        for a in control["automations"]
    ), control


@pytest.mark.asyncio
async def test_flow_helper_search_warns_about_the_token(
    mcp_client, ha_client: HomeAssistantClient, non_admin_client: HomeAssistantClient
):
    """Flow-helper options probes use an admin-only route; refusals warn too."""
    name = "NonAdmin Probe MinMax E2E"
    created = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "min_max",
                "name": name,
                "config": {
                    "name": name,
                    "entity_ids": [
                        "sensor.demo_temperature",
                        "sensor.demo_outside_temperature",
                    ],
                    "type": "min",
                },
            },
        ),
        "Create min_max helper",
    )
    entry_id = created.get("entry_id")
    assert entry_id
    try:
        result = await SmartSearchTools(client=non_admin_client).deep_search(
            name, search_types=["helper"], limit=10, include_config=True
        )
        assert result.get("partial") is True, result
        assert NON_ADMIN_TOKEN_WARNING in result.get("warnings", []), result

        control = await SmartSearchTools(client=ha_client).deep_search(
            name, search_types=["helper"], limit=10, include_config=True
        )
        assert NON_ADMIN_TOKEN_WARNING not in control.get("warnings", []), control
        assert any(h.get("entry_id") == entry_id for h in control["helpers"]), control
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )
