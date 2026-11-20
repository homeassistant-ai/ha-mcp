"""Exercise the E2E cleanup helpers against the real File & YAML Tools entry."""

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from tests.src.e2e.utilities import component_services

from ._core_setup import set_up_tools_entry


@pytest.fixture(autouse=True)
def admin_token(hass_access_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(component_services, "TEST_TOKEN", hass_access_token)


@pytest.mark.parametrize(
    ("setter", "getter", "field", "values"),
    [
        (
            component_services.set_allowed_paths,
            "get_allowed_paths",
            "paths",
            ["pyscript"],
        ),
        (
            component_services.set_extra_yaml_keys,
            "get_extra_yaml_keys",
            "keys",
            ["test_key"],
        ),
    ],
)
async def test_a_successful_reset_updates_the_component(
    hass: HomeAssistant, core_client: HomeAssistantClient, setter, getter, field, values
) -> None:
    await set_up_tools_entry(hass)
    url = core_client.base_url
    token = await component_services.bootstrap_caller_token(url)
    assert token
    result = await setter(url, token, values)
    assert result["success"] is True
    response = await component_services.post_service(
        url, core_client.token, getter, {"_ha_mcp_token": token}
    )
    assert response.json()["service_response"][field] == values


@pytest.mark.parametrize("installed", [True, False])
async def test_a_failed_reset_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient, installed: bool
) -> None:
    if installed:
        await set_up_tools_entry(hass)
    with pytest.raises(AssertionError, match="set_extra_yaml_keys failed"):
        await component_services.set_extra_yaml_keys(
            core_client.base_url, "invalid", []
        )


async def test_bootstrap_names_the_missing_service(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(AssertionError, match="get_caller_token failed"):
        await component_services.bootstrap_caller_token(core_client.base_url)
