"""Native service-catalog shaping: translations, sections, filters and response.

The fixture is real Home Assistant 2026.11.0 output, so these tests fail when the
server stops passing through a contract Core already serves. The defect they
pin: ``ha_list_services`` advertised title-cased names, empty descriptions and a
hand-derived ``type`` while Core supplied translated text, section fields,
per-field ``filter`` and the service's ``response`` shape.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import component_api
from ha_mcp.tools.tools_services import _process_services, register_services_tools

from ._services_catalog_fixture import SERVICES, TRANSLATIONS
from .test_component_ws_search import FakeHass


def _process(
    *,
    domain: str | None = None,
    query: str | None = None,
    detail_level: str = "full",
    translations: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _process_services(
        rest_services=SERVICES,
        translations=TRANSLATIONS if translations is None else translations,
        domain_filter=domain,
        query_filter=query,
        limit=200,
        offset=0,
        detail_level=detail_level,  # type: ignore[arg-type]
    )


def _service(result: dict[str, Any], key: str) -> dict[str, Any]:
    return result["services"][key]


def test_service_name_and_description_come_from_flat_translations() -> None:
    turn_on = _service(_process(domain="light"), "light.turn_on")

    assert turn_on["name"] == "Turn on light"
    assert turn_on["description"].startswith("Turns on one or more lights")


def test_field_name_and_description_come_from_flat_translations() -> None:
    fields = _service(_process(domain="light"), "light.turn_on")["fields"]

    assert fields["brightness_pct"]["name"] == "Brightness"
    assert fields["brightness_pct"]["description"].startswith(
        "Number indicating the percentage of full brightness"
    )


def test_core_name_and_description_survive_when_no_translation_exists() -> None:
    """Dynamic services carry their text in Core's description, not strings.json."""
    script = _service(_process(domain="script"), "script.on_the_way_home")

    assert script["name"] == "On the Way Home"
    assert script["description"].startswith("Notifies both phones")


def test_title_case_is_only_the_last_resort() -> None:
    turn_off = _service(_process(domain="switch"), "switch.turn_off")

    assert turn_off["name"] == "Turn Off"
    assert turn_off["description"] == ""


def test_description_placeholders_resolve_in_field_text() -> None:
    fields = _service(_process(domain="zwave_js"), "zwave_js.invoke_cc_api")["fields"]

    description = fields["method_name"]["description"]
    assert "https://zwave-js.github.io/node-zwave-js/" in description
    assert "{api_docs_url}" not in description


def test_unknown_placeholders_and_literal_braces_survive() -> None:
    translations = copy.deepcopy(TRANSLATIONS)
    translations[
        "component.zwave_js.services.invoke_cc_api.fields.method_name.description"
    ] = 'Docs at {api_docs_url}; schema {"a": 1}; missing {other_placeholder}.'

    fields = _service(
        _process(domain="zwave_js", translations=translations),
        "zwave_js.invoke_cc_api",
    )["fields"]

    description = fields["method_name"]["description"]
    assert "Docs at https://zwave-js.github.io/node-zwave-js/" in description
    assert '{"a": 1}' in description
    assert "{other_placeholder}" in description


def test_service_response_shape_is_passed_through() -> None:
    assert _service(_process(domain="calendar"), "calendar.get_events")["response"] == {
        "optional": False
    }
    assert _service(_process(domain="conversation"), "conversation.process")[
        "response"
    ] == {"optional": True}
    assert _service(_process(domain="script"), "script.on_the_way_home")[
        "response"
    ] == {"optional": True}


def test_summary_keeps_response_but_omits_fields() -> None:
    get_events = _service(
        _process(domain="calendar", detail_level="summary"), "calendar.get_events"
    )

    assert get_events["response"] == {"optional": False}
    assert "fields" not in get_events


def test_section_entry_keeps_nested_fields_and_collapsed() -> None:
    fields = _service(_process(domain="light"), "light.turn_on")["fields"]
    section = fields["additional_fields"]

    assert section["name"] == "Additional options"
    assert section["collapsed"] is True
    assert set(section["fields"]) == {"rgbw_color", "color_name"}

    rgbw = section["fields"]["rgbw_color"]
    assert rgbw["name"] == "RGBW-color"
    assert rgbw["description"].startswith("The color in RGBW format")
    assert rgbw["example"] == "[255, 100, 100, 50]"
    assert rgbw["selector"] == {"object": {"multiple": False}}
    assert rgbw["filter"]["attribute"]["supported_color_modes"]


def test_section_without_collapsed_keeps_only_what_core_sent() -> None:
    fields = _service(_process(domain="climate"), "climate.set_temperature")["fields"]
    section = fields["temperature_range"]

    assert section["name"] == "Temperature range"
    assert "collapsed" not in section
    assert section["fields"]["target_temp_high"]["name"] == "Upper target temperature"


def test_field_filter_is_passed_through() -> None:
    transition = _service(_process(domain="light"), "light.turn_on")["fields"][
        "transition"
    ]

    assert transition["filter"] == {"supported_features": [32]}
    assert transition["selector"]["number"]["unit_of_measurement"] == "seconds"


def test_false_valued_advanced_and_default_are_not_dropped() -> None:
    fields = _service(_process(domain="script"), "script.voice_play_music")["fields"]

    assert fields["radio_mode"]["advanced"] is False
    assert fields["radio_mode"]["default"] is False


def test_required_is_reported_only_when_core_sends_it() -> None:
    light_fields = _service(_process(domain="light"), "light.turn_on")["fields"]
    conversation_fields = _service(
        _process(domain="conversation"), "conversation.process"
    )["fields"]
    fan_fields = _service(_process(domain="fan"), "fan.increase_speed")["fields"][
        "additional_fields"
    ]["fields"]

    assert "required" not in light_fields["transition"]
    assert conversation_fields["text"]["required"] is True
    assert fan_fields["percentage_step"]["required"] is False


def test_derived_type_and_synthetic_example_are_gone() -> None:
    fields = _service(_process(domain="light"), "light.turn_on")["fields"]

    for field in fields.values():
        assert "type" not in field
        if "fields" in field:
            for nested in field["fields"].values():
                assert "type" not in nested

    assert "example" not in fields["brightness_pct"]
    assert "example" not in fields["transition"]


def test_select_options_are_not_flattened_or_truncated() -> None:
    options = _service(_process(domain="light"), "light.turn_on")["fields"][
        "additional_fields"
    ]["fields"]["color_name"]["selector"]["select"]["options"]

    assert len(options) == 6
    assert options[0] == {"value": "red", "label": "Red"}


def test_query_matches_translated_descriptions() -> None:
    result = _process(query="turns on one or more lights")

    assert "light.turn_on" in result["services"]


@pytest.mark.asyncio
async def test_legacy_translations_request_uses_configured_language() -> None:
    client = _LegacyClient(language="de")

    await _build_list_services(client)()

    assert client.translation_messages == [
        {
            "type": "frontend/get_translations",
            "language": "de",
            "category": "services",
        }
    ]


@pytest.mark.asyncio
async def test_unreadable_config_uses_english_and_warns() -> None:
    client = _LegacyClient(raise_on_config=True)

    result = await _build_list_services(client)()

    assert client.translation_messages[0]["language"] == "en"
    assert any("language" in warning for warning in result.get("warnings", []))


@pytest.mark.asyncio
async def test_config_without_a_language_uses_english_and_warns() -> None:
    client = _LegacyClient(config={"time_zone": "UTC"})

    result = await _build_list_services(client)()

    assert client.translation_messages[0]["language"] == "en"
    assert any("language" in warning for warning in result.get("warnings", []))


def test_null_core_description_normalizes_to_empty_string() -> None:
    """Core serves ``description: null`` for some ESPHome fields; keep a string."""
    catalog = [
        {
            "domain": "esphome",
            "services": {
                "led": {
                    "description": None,
                    "fields": {
                        "red": {"description": None, "selector": {"number": {}}}
                    },
                }
            },
        }
    ]

    result = _process_services(
        rest_services=catalog,
        translations={},
        limit=200,
        detail_level="full",
    )

    service = result["services"]["esphome.led"]
    assert service["description"] == ""
    assert service["fields"]["red"]["description"] == ""


def test_drifted_field_payloads_are_skipped_not_fatal() -> None:
    """A shape drift in one field must not take the whole catalog down."""
    catalog = [
        {
            "domain": "drift",
            "services": {
                "not_a_mapping": {"fields": "not-a-mapping"},
                "mixed": {
                    "fields": {
                        "good": {"selector": {}},
                        "bad": "not-a-mapping",
                    }
                },
            },
        }
    ]

    result = _process_services(
        rest_services=catalog,
        translations={},
        limit=200,
        detail_level="full",
    )

    assert result["services"]["drift.not_a_mapping"]["fields"] == {}
    assert set(result["services"]["drift.mixed"]["fields"]) == {"good"}


@pytest.mark.asyncio
async def test_component_prep_uses_home_assistant_configured_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools.websocket_api import (
        services as component_services,
    )

    seen: list[str] = []

    async def _descriptions(hass: Any) -> dict[str, Any]:
        return {}

    async def _translations(hass: Any, language: str) -> dict[str, Any]:
        seen.append(language)
        return {}

    monkeypatch.setattr(
        component_services, "_fetch_service_descriptions", _descriptions
    )
    monkeypatch.setattr(
        component_services, "_fetch_service_translations", _translations
    )

    await component_services._services_list_prep(
        FakeHass(config=SimpleNamespace(language="de")), {}
    )

    assert seen == ["de"]


class _LegacyClient:
    """A client without the component, so the REST + translation path runs."""

    def __init__(
        self,
        *,
        language: str | None = "en",
        config: dict[str, Any] | None = None,
        raise_on_config: bool = False,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.config = (
            config
            if config is not None
            else ({"language": language} if language else None)
        )
        self.raise_on_config = raise_on_config
        self.translation_messages: list[dict[str, Any]] = []
        component_api._store_caps(self, None)

    async def get_config(self) -> dict[str, Any]:
        if self.raise_on_config:
            raise HomeAssistantAPIError("config unavailable")
        return self.config or {}

    async def get_services(self) -> list[dict[str, Any]]:
        return SERVICES

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.translation_messages.append(dict(msg))
        return {"success": True, "result": {"resources": TRANSLATIONS}}


def _build_list_services(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = type("MCP", (), {"add_tool": staticmethod(capture_add_tool)})()
    register_services_tools(mcp, client)
    return registered["ha_list_services"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
