"""The component's integration_commands bridge follows Core's command registry."""

import sys
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

from .test_component_ws_search import _REAL_VOL, FakeHass


def _execute() -> Any:
    from custom_components.ha_mcp_tools import core_contract

    return next(
        execute
        for schema, execute, _prep in core_contract.command_specs(_REAL_VOL)
        if schema[_REAL_VOL.Required("type")] == core_contract.INTEGRATION_COMMAND
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
        knx__delete_entity=_REAL_VOL.Schema({"entity_id": str}),
        knx__get_base_data=_REAL_VOL.Schema({"future": int}),
        knxx__other=_REAL_VOL.Schema({}),
        config__core__update=_REAL_VOL.Schema({}),
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
    messages.BASE_COMMAND_MESSAGE_SCHEMA = _REAL_VOL.Schema(  # type: ignore[attr-defined]
        {_REAL_VOL.Required("id"): int}
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.websocket_api.messages", messages
    )
    hass = _hass(knx__get_knx_project=False)
    listing = _execute()(hass, {"domain": "knx"})
    assert set(listing["commands"][0]["schema"]["described"]) == {
        _REAL_VOL.Required("id"),
        "type",
    }
    rejected = _execute()(
        hass, {"domain": "knx", "command": "knx/get_knx_project", "payload": {"x": 1}}
    )
    assert rejected["valid"] is False
    accepted = _execute()(
        hass, {"domain": "knx", "command": "knx/get_knx_project", "payload": {}}
    )
    assert accepted["valid"] is True


def test_validation_uses_the_registered_schema_without_running_the_handler() -> None:
    handler = Mock(side_effect=AssertionError("validation must never dispatch"))
    hass = FakeHass()
    hass.data["websocket_api"] = {
        "knx/delete_entity": (
            handler,
            _REAL_VOL.Schema(
                {
                    _REAL_VOL.Required("id"): int,
                    _REAL_VOL.Required("type"): str,
                    _REAL_VOL.Required("entity_id"): str,
                }
            ),
        )
    }
    msg: dict[str, Any] = {"domain": "knx", "command": "knx/delete_entity"}
    invalid = _execute()(hass, {**msg, "payload": {}})
    assert invalid["status"] == "validated"
    assert invalid["valid"] is False
    assert invalid["errors"][0]["path"] == ["entity_id"]
    valid = _execute()(hass, {**msg, "payload": {"entity_id": "light.x"}})
    assert valid["valid"] is True
    handler.assert_not_called()


def test_a_command_outside_the_domain_prefix_is_refused() -> None:
    hass = _hass(config__core__update=_REAL_VOL.Schema({}))
    with pytest.raises(ValueError, match="knx/"):
        _execute()(
            hass, {"domain": "knx", "command": "config/core/update", "payload": {}}
        )


def test_an_unregistered_command_is_unavailable_not_valid() -> None:
    result = _execute()(
        _hass(), {"domain": "knx", "command": "knx/gone", "payload": {}}
    )
    assert result["status"] == "unavailable"


def test_info_advertises_integration_commands() -> None:
    from custom_components.ha_mcp_tools import websocket_api as wsapi

    assert "integration_commands" in wsapi._do_info(FakeHass())["capabilities"]
