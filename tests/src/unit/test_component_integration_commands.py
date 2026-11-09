"""The component's integration_commands bridge follows Core's command registry."""

import sys
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest
import voluptuous as vol

from .test_component_ws_search import FakeHass


def _execute() -> Any:
    from custom_components.ha_mcp_tools import core_contract

    return next(
        execute
        for schema, execute, _prep in core_contract.command_specs(vol)
        if schema[vol.Required("type")] == core_contract.INTEGRATION_COMMAND
    )


def _hass(**commands: Any) -> FakeHass:
    hass = FakeHass()
    hass.data["websocket_api"] = {
        name.replace("__", "/"): (Mock(), schema) for name, schema in commands.items()
    }
    return hass


@pytest.fixture
def serializer(monkeypatch: pytest.MonkeyPatch) -> Mock:
    from custom_components.ha_mcp_tools import core_contract

    serialize = Mock(side_effect=lambda schema, **_kw: {"described": schema.schema})
    monkeypatch.setattr(core_contract, "_TO_JSON_SCHEMA", serialize)
    return serialize


def test_lists_only_the_domains_own_commands_with_their_schemas(
    serializer: Mock,
) -> None:
    hass = _hass(
        knx__delete_entity=vol.Schema({"entity_id": str}),
        knx__get_base_data=vol.Schema({"future": int}),
        knxx__other=vol.Schema({}),
        config__core__update=vol.Schema({}),
    )
    result = _execute()(hass, {"domain": "knx"})
    assert result["status"] == "available"
    assert [row["command"] for row in result["commands"]] == [
        "knx/delete_entity",
        "knx/get_base_data",
    ]
    assert result["commands"][1]["schema"] == {"described": {"future": int}}
    assert result["commands"][1]["description_complete"] is True


def test_a_command_without_fields_is_described_with_cores_message_schema(
    monkeypatch: pytest.MonkeyPatch, serializer: Mock
) -> None:
    messages = ModuleType("messages")
    messages.BASE_COMMAND_MESSAGE_SCHEMA = vol.Schema(  # type: ignore[attr-defined]
        {vol.Required("id"): int}
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.websocket_api.messages", messages
    )
    hass = _hass(knx__get_knx_project=False)
    listing = _execute()(hass, {"domain": "knx"})
    assert set(listing["commands"][0]["schema"]["described"]) == {
        vol.Required("id"),
        "type",
    }


def test_info_advertises_integration_commands() -> None:
    from custom_components.ha_mcp_tools import websocket_api as wsapi

    assert "integration_commands" in wsapi._do_info(FakeHass())["capabilities"]
