"""The component's catalog commands on a real ``hass``.

``services_list`` is Core's ``/api/services`` catalog (with translations),
``reference_data`` the service index and entity ids the reference checks of
older servers read, and ``server_entry`` the component's own server config
entry. Services come from Core's ``input_boolean`` and ``counter``
integrations; entries are ``MockConfigEntry`` rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.api import async_services_json
from homeassistant.core import HomeAssistant
from homeassistant.helpers import service as core_service
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.const import (
    DATA_LAST_PIP_SPEC,
    DOMAIN,
    OPT_PIP_SPEC,
)
from custom_components.ha_mcp_tools.spec_dist import async_paired_server_requirement

from ._component_core import ws_call, ws_result
from ._server_entry import add_server_entry


@pytest.fixture
async def services(hass: HomeAssistant) -> None:
    for domain in ("input_boolean", "counter"):
        assert await async_setup_component(hass, domain, {})


# --- services_list ----------------------------------------------------------------


@pytest.mark.usefixtures("services")
async def test_the_service_catalog_is_cores_api_services(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    result = await ws_result(component_ws, "ha_mcp_tools/services_list")

    assert result["services"] == await async_services_json(hass)


@pytest.mark.usefixtures("services")
async def test_a_domain_keeps_only_its_services_and_translations(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    result = await ws_result(
        component_ws, "ha_mcp_tools/services_list", domain="counter"
    )

    assert [entry["domain"] for entry in result["services"]] == ["counter"]
    assert result["translations"]
    assert {key.split(".")[1] for key in result["translations"]} == {"counter"}


@pytest.mark.parametrize(
    "bad",
    [{"query": "light"}, {"domain": 5}, {"language": 5}],
    ids=["query", "domain", "language"],
)
async def test_a_malformed_catalog_frame_is_refused(
    component_ws: MockHAClientWebSocket, bad: dict[str, Any]
) -> None:
    """``query`` is no parameter: a coarse filter here could not be a superset
    of the server's own, so it would drop services the server keeps."""
    reply = await ws_call(component_ws, "ha_mcp_tools/services_list", **bad)

    assert reply["error"]["code"] == "invalid_format"


@pytest.mark.usefixtures("services")
async def test_descriptions_core_no_longer_maps_fail_the_read(
    component_ws: MockHAClientWebSocket,
) -> None:
    """Another Core's descriptions shape is an error the server falls back on,
    not an empty catalog it would trust."""
    with patch.object(
        core_service,
        "async_get_all_descriptions",
        AsyncMock(return_value=["not", "a", "mapping"]),
    ):
        reply = await ws_call(component_ws, "ha_mcp_tools/services_list")

    assert reply["success"] is False


# --- reference_data ---------------------------------------------------------------


@pytest.mark.usefixtures("services")
async def test_reference_data_names_every_service_and_entity(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")
    hass.states.async_set("sensor.b", "1")

    result = await ws_result(component_ws, "ha_mcp_tools/reference_data")

    assert {e["domain"]: set(e["services"]) for e in result["services"]} == {
        domain: set(names) for domain, names in hass.services.async_services().items()
    }
    assert all(
        body == {} for e in result["services"] for body in e["services"].values()
    )
    assert set(result["entity_ids"]) == set(hass.states.async_entity_ids())


@pytest.mark.usefixtures("services")
async def test_reference_data_without_states_still_names_the_services(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")

    result = await ws_result(
        component_ws, "ha_mcp_tools/reference_data", include_states=False
    )

    assert result["entity_ids"] == []
    assert result["services"]


async def test_a_malformed_reference_frame_is_refused(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await ws_call(
        component_ws, "ha_mcp_tools/reference_data", include_states="yes"
    )

    assert reply["error"]["code"] == "invalid_format"


# --- server_entry -----------------------------------------------------------------


async def test_the_server_entry_is_found_by_its_marker_alone(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Only its id, pip_spec and whether that spec is installed come back:
    neither its data nor the retired channel option."""
    MockConfigEntry(domain=DOMAIN, data={"entry_type": "tools"}).add_to_hass(hass)
    MockConfigEntry(domain="some_other", data={"entry_type": "server"}).add_to_hass(
        hass
    )
    server = add_server_entry(
        hass,
        data={"access_token": "TOKEN"},
        options={"channel": "dev", OPT_PIP_SPEC: "ha-mcp==1.0"},
    )

    result = await ws_result(component_ws, "ha_mcp_tools/server_entry")

    # Core's test harness starts Home Assistant with skip_pip, where the
    # component installs nothing, so whether the spec is installed is unknown.
    assert result == {
        "entry_id": server.entry_id,
        "pip_spec": "ha-mcp==1.0",
        "install_pending": None,
    }


async def test_no_server_entry_is_an_answer_not_an_error(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With pip on, as in production, where a tools-only install has no
    # server entry whose spec could be pending.
    monkeypatch.setattr(hass.config, "skip_pip", False)
    MockConfigEntry(domain=DOMAIN, data={"entry_type": "tools"}).add_to_hass(hass)
    MockConfigEntry(domain="some_other", data={"entry_type": "server"}).add_to_hass(
        hass
    )

    result = await ws_result(component_ws, "ha_mcp_tools/server_entry")

    assert result == {"entry_id": None, "pip_spec": None, "install_pending": None}


COMMIT_A = "ha-mcp @ git+https://github.com/example/ha-mcp@" + "a" * 40
COMMIT_B = "ha-mcp @ git+https://github.com/example/ha-mcp@" + "b" * 40
PIN = "the manifest pin"


@pytest.mark.parametrize(
    ("override", "installed", "pending"),
    [
        pytest.param(COMMIT_B, COMMIT_A, True, id="switched-not-installed"),
        pytest.param(COMMIT_B, COMMIT_B, False, id="switched-and-installed"),
        pytest.param("", COMMIT_A, True, id="cleared-not-installed"),
        pytest.param("", PIN, False, id="pin-installed"),
    ],
)
async def test_a_spec_still_to_be_installed_is_reported_pending(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    installed: str,
    pending: bool,
) -> None:
    """A deferred install leaves the new spec in the options while the old
    build serves; without the flag that reads as a finished switch."""
    monkeypatch.setattr(hass.config, "skip_pip", False)
    pin = await async_paired_server_requirement(hass)
    add_server_entry(
        hass,
        data={DATA_LAST_PIP_SPEC: pin if installed == PIN else installed},
        options={OPT_PIP_SPEC: override},
    )

    result = await ws_result(component_ws, "ha_mcp_tools/server_entry")

    assert result["install_pending"] is pending


async def test_a_server_entry_frame_with_extra_keys_is_refused(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await ws_call(component_ws, "ha_mcp_tools/server_entry", entry_id="x")

    assert reply["error"]["code"] == "invalid_format"


# --- the admin gate ---------------------------------------------------------------

_COMMANDS = {
    "ha_mcp_tools/dashboards": {"mode": "list"},
    "ha_mcp_tools/services_list": {},
    "ha_mcp_tools/reference_data": {},
    "ha_mcp_tools/server_entry": {},
    "ha_mcp_tools/call_event": {"event_type": "probe_event"},
}


@pytest.mark.parametrize("command", sorted(_COMMANDS))
async def test_only_an_admin_gets_an_answer(
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
    command: str,
) -> None:
    refused = await ws_call(component_ws_read_only, command, **_COMMANDS[command])
    answered = await ws_call(component_ws, command, **_COMMANDS[command])

    assert refused["error"]["code"] == "unauthorized"
    assert answered["success"] is True
