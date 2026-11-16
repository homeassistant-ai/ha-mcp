"""The ha_mcp_tools caller-token auth path on a real ``hass``.

Three layers, all against the real tools entry that Core sets up:

1. The component's services refuse a call that does not carry its token.
2. ``call_mcp_tools_service`` fetches, caches and re-fetches the token over the
   server's own client, and gates on the component's reported version.
3. ``ha_call_service`` refuses the ``ha_mcp_tools`` domain.

The component's version is the manifest version Core's loader holds, so a test
changes what the component reports by changing that.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, patch

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.loader import async_get_integration
from pytest_homeassistant.common import async_capture_events

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_DELETE_FILE,
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_LIST_FILES,
    SERVICE_SET_ALLOWED_PATHS,
    SERVICE_SET_EXTRA_YAML_KEYS,
    SERVICE_WRITE_FILE,
)
from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.exceptions import HomeAssistantConnectionError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings
from ha_mcp.tools.tools_filesystem import (
    MIN_COMPONENT_VERSION,
    call_mcp_tools_service,
)
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.yaml_extra_keys import effective_extra_yaml_write_keys

from ._file_services import FileService, make_file_service

# Where the component keeps its token (``.storage/ha_mcp_tools_auth``).
TOKEN_STORE_KEY = f"{DOMAIN}_auth"


@pytest.fixture
async def tools_entry(hass: HomeAssistant) -> FileService:
    """The tools entry, set up by Core; the caller presents its token."""
    return await make_file_service(hass)


@pytest.fixture
def service_calls(core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the client's ``call_service``: it calls through, Core answers."""
    with patch.object(
        core_client, "call_service", wraps=core_client.call_service
    ) as spy:
        yield spy


async def raw_call(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Call an ``ha_mcp_tools`` service with exactly ``data``."""
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    assert response is not None
    return dict(response)


async def bootstrap_token(hass: HomeAssistant) -> str:
    """The token the component hands out through its bootstrap service."""
    reply = await raw_call(hass, SERVICE_GET_CALLER_TOKEN, {})
    token = reply["token"]
    assert isinstance(token, str)
    return token


async def set_component_version(hass: HomeAssistant, version: str | None) -> None:
    """Change the version the component reports; ``None`` removes it."""
    manifest = (await async_get_integration(hass, DOMAIN)).manifest
    if version is None:
        del manifest["version"]
    else:
        manifest["version"] = version


def bootstrap_calls(spy: AsyncMock) -> list[Any]:
    """The bootstrap calls the client made, from a ``call_service`` spy."""
    return [c for c in spy.await_args_list if c.args[1] == SERVICE_GET_CALLER_TOKEN]


# --- The component's services refuse a call without the token ---------------


async def test_the_bootstrap_token_is_accepted(
    hass: HomeAssistant, tools_entry: FileService
) -> None:
    """The token the component hands out opens its gated services."""
    token = await bootstrap_token(hass)

    reply = await raw_call(hass, SERVICE_GET_ALLOWED_PATHS, {CALLER_TOKEN_FIELD: token})

    assert reply["success"] is True


@pytest.mark.parametrize(
    "presented",
    [
        {},
        {CALLER_TOKEN_FIELD: ""},
        {CALLER_TOKEN_FIELD: "not-the-token"},
        {CALLER_TOKEN_FIELD: 123},
    ],
    ids=["missing", "empty", "wrong", "number"],
)
async def test_a_missing_or_wrong_token_is_refused(
    hass: HomeAssistant, tools_entry: FileService, presented: dict[str, Any]
) -> None:
    """Anyone but ha-mcp is told ``unauthorized`` and gets no data."""
    reply = await raw_call(hass, SERVICE_GET_ALLOWED_PATHS, presented)

    assert reply["success"] is False
    assert reply["error_code"] == "unauthorized"
    assert reply["paths"] == []


@pytest.mark.parametrize(
    ("service", "data", "extra"),
    [
        (SERVICE_LIST_FILES, {"path": "www"}, {"files": []}),
        (SERVICE_DELETE_FILE, {"path": "www/x"}, {}),
        (SERVICE_GET_EXTRA_YAML_KEYS, {}, {"keys": []}),
    ],
)
async def test_the_refusal_names_the_service_and_keeps_the_reply_keys(
    hass: HomeAssistant,
    tools_entry: FileService,
    service: str,
    data: dict[str, Any],
    extra: dict[str, Any],
) -> None:
    """The client tells a refusal from a failure by ``error_code``, and a list
    service still carries its empty list so callers need no special case."""
    reply = await raw_call(hass, service, {**data, CALLER_TOKEN_FIELD: "bad"})

    assert reply["error_code"] == "unauthorized"
    assert service in reply["error"]
    for key, value in extra.items():
        assert reply[key] == value


@pytest.mark.parametrize(
    ("write", "data", "read", "field"),
    [
        (
            SERVICE_SET_ALLOWED_PATHS,
            {"paths": ["pyscript"]},
            SERVICE_GET_ALLOWED_PATHS,
            "paths",
        ),
        (
            SERVICE_SET_EXTRA_YAML_KEYS,
            {"keys": ["alert2"]},
            SERVICE_GET_EXTRA_YAML_KEYS,
            "keys",
        ),
    ],
    ids=["paths", "keys"],
)
async def test_a_refused_settings_write_changes_nothing(
    hass: HomeAssistant,
    tools_entry: FileService,
    write: str,
    data: dict[str, Any],
    read: str,
    field: str,
) -> None:
    """A caller without the token cannot widen what ha-mcp may write."""
    refused = await raw_call(hass, write, {**data, CALLER_TOKEN_FIELD: "bad"})
    assert refused["error_code"] == "unauthorized"
    assert (await tools_entry(read, {}))[field] == []

    accepted = await tools_entry(write, data)

    assert accepted["success"] is True
    assert (await tools_entry(read, {}))[field] == next(iter(data.values()))


# --- The version the bootstrap reports --------------------------------------


async def test_the_bootstrap_reports_the_manifest_version(
    hass: HomeAssistant, tools_entry: FileService
) -> None:
    token = await bootstrap_token(hass)
    await set_component_version(hass, "1.2.4")

    reply = await raw_call(hass, SERVICE_GET_CALLER_TOKEN, {})

    assert reply == {"success": True, "token": token, "version": "1.2.4"}


async def test_a_manifest_without_a_version_is_unreadable_and_leaks_no_token(
    hass: HomeAssistant, tools_entry: FileService
) -> None:
    """A missing version must not become the string ``"None"`` that the
    server's gate then compares; the token stays out of a failure reply."""
    await set_component_version(hass, None)

    reply = await raw_call(hass, SERVICE_GET_CALLER_TOKEN, {})

    assert reply["success"] is False
    assert reply["error_code"] == "manifest_unreadable"
    assert "token" not in reply


# --- call_mcp_tools_service on the server's client --------------------------


async def test_the_first_call_bootstraps_then_sends_the_token(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    service_calls: AsyncMock,
) -> None:
    token = await bootstrap_token(hass)

    result = await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})

    first, second = service_calls.await_args_list
    assert first.args[1] == SERVICE_GET_CALLER_TOKEN
    assert second.args[2][CALLER_TOKEN_FIELD] == token
    assert result["service_response"]["success"] is True


async def test_the_token_is_fetched_once_for_many_calls(
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    service_calls: AsyncMock,
) -> None:
    first = await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})
    second = await call_mcp_tools_service(core_client, SERVICE_GET_EXTRA_YAML_KEYS, {})

    assert len(bootstrap_calls(service_calls)) == 1
    assert first["service_response"]["success"] is True
    assert second["service_response"]["success"] is True


async def test_a_rotated_token_is_refetched_and_the_call_retried_once(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    service_calls: AsyncMock,
) -> None:
    """After the component's token is regenerated, ha-mcp's cached one is
    stale: it must refetch and retry instead of failing every file tool."""
    await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage.pop(TOKEN_STORE_KEY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fresh = await bootstrap_token(hass)
    service_calls.reset_mock()

    result = await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})

    assert result["service_response"]["success"] is True
    assert [c.args[1] for c in service_calls.await_args_list] == [
        SERVICE_GET_ALLOWED_PATHS,
        SERVICE_GET_CALLER_TOKEN,
        SERVICE_GET_ALLOWED_PATHS,
    ]
    assert service_calls.await_args_list[-1].args[2][CALLER_TOKEN_FIELD] == fresh


async def test_a_token_that_stays_stale_is_retried_once_and_then_refused(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    service_calls: AsyncMock,
) -> None:
    """When the retry is refused too, the caller gets the component's
    ``unauthorized`` after exactly two attempts, not a loop."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)

    async def rotate_before_protected_call(
        domain: str, service: str, *_: Any, **__: Any
    ) -> Any:
        if service != SERVICE_GET_CALLER_TOKEN:
            assert await hass.config_entries.async_unload(entry.entry_id)
            hass_storage.pop(TOKEN_STORE_KEY)
            assert await hass.config_entries.async_setup(entry.entry_id)
        return DEFAULT

    service_calls.side_effect = rotate_before_protected_call

    result = await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})

    assert result["service_response"]["error_code"] == "unauthorized"
    attempts = [
        c
        for c in service_calls.await_args_list
        if c.args[1] == SERVICE_GET_ALLOWED_PATHS
    ]
    assert len(attempts) == 2


async def test_a_component_without_the_bootstrap_service_gets_an_update_prompt(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    service_calls: AsyncMock,
) -> None:
    """Domain services present but no ``get_caller_token``: the user is sent to
    update the component, not shown Home Assistant's opaque 400."""
    hass.services.async_remove(DOMAIN, SERVICE_GET_CALLER_TOKEN)

    with pytest.raises(ToolError) as exc_info:
        await call_mcp_tools_service(core_client, SERVICE_LIST_FILES, {"path": "www"})

    msg = str(exc_info.value)
    assert "too old" in msg
    assert "pre-0.5.0" in msg
    service_calls.assert_not_called()


async def test_a_missing_tools_entry_is_told_to_add_the_entry_not_to_update(
    core_client: HomeAssistantClient, service_calls: AsyncMock
) -> None:
    """With only the server entry added there are no ``ha_mcp_tools`` services,
    and a HACS update cannot fix that (#1996)."""
    with pytest.raises(ToolError) as exc_info:
        await call_mcp_tools_service(core_client, SERVICE_LIST_FILES, {"path": "www"})

    msg = str(exc_info.value)
    assert "HA-MCP File & YAML Tools" in msg
    assert "Add entry" in msg
    assert "too old" not in msg
    assert msg.index("Add entry") < msg.index("ha_manage_hacs")
    service_calls.assert_not_called()


async def test_the_component_reason_is_surfaced_verbatim(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools_entry: FileService
) -> None:
    """An unreadable manifest must not be re-diagnosed as a missing token, whose
    remedies (reload, admin rights) cannot fix a manifest."""
    await set_component_version(hass, None)

    with pytest.raises(ToolError, match="manifest version could not be read"):
        await call_mcp_tools_service(core_client, SERVICE_LIST_FILES, {"path": "www"})


@pytest.mark.parametrize(
    "version",
    [MIN_COMPONENT_VERSION, f"{int(MIN_COMPONENT_VERSION.split('.')[0]) + 1}.0.0"],
    ids=["minimum", "above"],
)
async def test_a_component_at_or_above_the_minimum_is_accepted(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    version: str,
) -> None:
    """The gate is ``<``, so exactly the minimum and a later release pass."""
    await set_component_version(hass, version)

    result = await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})

    assert result["service_response"]["success"] is True


@pytest.mark.parametrize("version", ["1.x.0", "1.2.3-beta"])
async def test_a_malformed_version_is_refused_without_an_update_prompt(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    version: str,
) -> None:
    """``1.x.0`` must not pass as ``(1, 0, 0)``; and updating cannot fix a
    manifest whose version is wrong, so the advice is to reinstall."""
    await set_component_version(hass, version)

    with pytest.raises(ToolError) as exc_info:
        await call_mcp_tools_service(core_client, SERVICE_LIST_FILES, {"path": "www"})

    msg = str(exc_info.value)
    assert "malformed version" in msg
    assert version in msg
    assert "Reinstall" in msg
    assert "too old" not in msg


# --- ha_call_service refuses the ha_mcp_tools domain ------------------------


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(core_client, None)


@pytest.mark.parametrize("domain", ["ha_mcp_tools", "HA_MCP_TOOLS", "  ha_mcp_tools  "])
async def test_ha_call_service_refuses_the_tools_domain_in_any_spelling(
    hass: HomeAssistant,
    tools: ServiceTools,
    tools_entry: FileService,
    service_calls: AsyncMock,
    domain: str,
) -> None:
    """Core lowercases the domain on its fallback lookup, so a spelling variant
    would otherwise reach the file services past the refusal."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_call_service(
            domain=domain, service=SERVICE_WRITE_FILE, data={"path": "www/x"}
        )

    msg = str(exc_info.value)
    assert "ha_mcp_tools" in msg
    assert any(
        name in msg for name in ("ha_write_file", "ha_list_files", "ha_config_set_yaml")
    )
    service_calls.assert_not_called()
    assert events == []


async def test_ha_call_service_still_reaches_other_domains(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """Only ``ha_mcp_tools`` is refused: the same call on another domain runs."""
    dispatched: list[dict[str, Any]] = []

    async def handle(call: ServiceCall) -> None:
        dispatched.append(dict(call.data))

    hass.services.async_register("test", "turn_on", handle)

    response = await tools.ha_call_service(
        domain="test", service="turn_on", entity_id="light.kitchen", wait=False
    )

    assert response["success"] is True
    assert dispatched == [{"entity_id": "light.kitchen"}]


# --- The write allowlist extension in force ---------------------------------


def _settings(env: str) -> Settings:
    return Settings(_env_file=None, HA_MCP_EXTRA_YAML_KEYS=env)


@pytest.mark.parametrize(
    ("env", "stored", "expected"),
    [
        (" alert2, foo ", [], ["alert2", "foo"]),
        ("alert2", ["beta"], ["alert2", "beta"]),
        ("", ["beta"], ["beta"]),
    ],
    ids=["env_only", "union", "store_only"],
)
async def test_the_effective_keys_are_the_server_setting_plus_the_component_store(
    core_client: HomeAssistantClient,
    tools_entry: FileService,
    env: str,
    stored: list[str],
    expected: list[str],
) -> None:
    """A key set only on the component would otherwise be refused by the
    server's own allowlist before the write ever reached the component."""
    await tools_entry(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": stored})

    keys = await effective_extra_yaml_write_keys(core_client, _settings(env))

    assert keys == expected


async def test_an_unreadable_component_store_falls_back_to_the_server_setting(
    core_client: HomeAssistantClient, tools_entry: FileService
) -> None:
    """A dropped connection while reading the store must not block a write that
    the server's own setting allows."""
    await call_mcp_tools_service(core_client, SERVICE_GET_ALLOWED_PATHS, {})

    with patch.object(
        core_client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    ):
        keys = await effective_extra_yaml_write_keys(core_client, _settings("alert2"))

    assert keys == ["alert2"]
