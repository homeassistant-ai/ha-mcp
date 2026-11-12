"""The tools entry's services on a real ``hass``: Core refuses a non-admin.

Core sets the entry up and unloads it through its config-entry machinery. Every ``ha_mcp_tools`` service registers through Core's admin-service helper,
so Core itself raises ``Unauthorized`` for a non-admin user before the handler
runs; the caller token then refuses an admin without it, and a call with no
user at all. Unload removes every service the setup registered.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools as comp
from custom_components.ha_mcp_tools import file_ops, yaml_find
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE


def assert_unauthorized(response: dict[str, Any] | None, service: str) -> None:
    """The reply ha-mcp's client reads as a refused caller token."""
    assert response is not None
    assert response["success"] is False
    assert response["error_code"] == "unauthorized"
    error = response["error"]
    assert isinstance(error, str)
    assert f"{DOMAIN}.{service}" in error


@pytest.fixture
async def tools_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_every_service_refuses_a_non_admin(
    hass: HomeAssistant, tools_entry: MockConfigEntry, hass_read_only_user: User
) -> None:
    services = hass.services.async_services_internal()[DOMAIN]
    assert services
    context = Context(user_id=hass_read_only_user.id)
    for name, service in services.items():
        # The handler is called directly: Core's schema check in async_call
        # would refuse the empty data before the admin check is reached.
        outcome = service.job.target(ServiceCall(hass, DOMAIN, name, {}, context))
        assert isinstance(outcome, Coroutine), name
        with pytest.raises(Unauthorized):
            await outcome


async def test_admin_with_a_wrong_token_gets_the_unauthorized_response(
    hass: HomeAssistant, tools_entry: MockConfigEntry, hass_admin_user: User
) -> None:
    response = await hass.services.async_call(
        DOMAIN,
        yaml_find.SERVICE_FIND_YAML_KEY,
        {"yaml_path": "template", comp.CALLER_TOKEN_FIELD: "not-the-token"},
        blocking=True,
        return_response=True,
        context=Context(user_id=hass_admin_user.id),
    )
    assert_unauthorized(response, yaml_find.SERVICE_FIND_YAML_KEY)


async def test_edit_file_is_response_only_with_its_schema(
    hass: HomeAssistant, tools_entry: MockConfigEntry
) -> None:
    data = {"path": "www/card.js", "edits": [{"old": "a", "new": "b"}]}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, file_ops.SERVICE_EDIT_FILE, data, blocking=True
        )
    with pytest.raises(Exception, match="edits"):
        await hass.services.async_call(
            DOMAIN,
            file_ops.SERVICE_EDIT_FILE,
            {"path": "www/card.js"},
            blocking=True,
            return_response=True,
        )
    response = await hass.services.async_call(
        DOMAIN, file_ops.SERVICE_EDIT_FILE, data, blocking=True, return_response=True
    )
    assert_unauthorized(response, file_ops.SERVICE_EDIT_FILE)


async def test_edit_file_refuses_a_path_outside_the_allowlist(
    hass: HomeAssistant, tools_entry: MockConfigEntry
) -> None:
    token_reply = await hass.services.async_call(
        DOMAIN,
        comp.SERVICE_GET_CALLER_TOKEN,
        {},
        blocking=True,
        return_response=True,
    )
    assert token_reply is not None
    token = token_reply["token"]
    response = await hass.services.async_call(
        DOMAIN,
        file_ops.SERVICE_EDIT_FILE,
        {
            "path": "configuration.yaml",
            "edits": [{"old": "a", "new": "b"}],
            comp.CALLER_TOKEN_FIELD: token,
        },
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert response["success"] is False
    error = response["error"]
    assert isinstance(error, str)
    assert "Write not allowed" in error


async def test_unload_removes_every_service(
    hass: HomeAssistant, tools_entry: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(tools_entry.entry_id) is True
    assert DOMAIN not in hass.services.async_services_internal()
