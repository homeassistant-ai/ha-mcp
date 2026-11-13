"""Service-catalog shaping: the server's own translation, section and fallback rules.

``ha_list_services`` reads the catalog from a real ``hass``. The services belong
to an integration whose ``services.yaml`` and English translations this file
writes, so Core loads both the way it loads any integration's: the catalog,
its translations and its description placeholders are Core's own. Core's shape
against the pinned image's ``kitchen_sink`` is asserted in
``tests/src/e2e/workflows/services/test_list_services.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.service import (
    async_get_all_descriptions,
    async_set_service_schema,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_services import ServiceDiscoveryTools

from ._client_spies import sent_messages

DOMAIN = "service_catalog"
DRIFT_DOMAIN = "service_catalog_drift"
SELECT_OPTIONS = ["red", "green", "blue", "yellow", "orange", "purple"]
PLACEHOLDERS = {"who": "you"}
LITERAL_BRACES = 'Runs for {who}; schema {"a": 1}; missing {other_placeholder}.'

SERVICES_YAML: dict[str, Any] = {
    "translated": {
        "fields": {
            "duration": {
                "selector": {
                    "number": {"min": 0, "max": 300, "unit_of_measurement": "seconds"}
                },
                "advanced": False,
                "default": False,
            },
            "mode": {
                "required": True,
                "selector": {
                    "select": {
                        "options": [
                            {"value": value, "label": value.title()}
                            for value in SELECT_OPTIONS
                        ]
                    }
                },
            },
            "section": {
                "collapsed": True,
                "fields": {"nested": {"selector": {"text": None}}},
            },
            "plain_section": {"fields": {"nested": {"selector": {"text": None}}}},
            "example_translated": {
                "example": "[1, 1]",
                "selector": {"object": {"multiple": False}},
            },
            "example_core": {"example": "2020-01-01", "selector": {"date": None}},
            "example_none": {"example": None, "selector": {"date": None}},
            "literal": {"selector": {"text": None}},
        }
    },
    "untranslated": {
        "name": "Core name",
        "description": "Core description.",
        "fields": {
            "core_field": {
                "name": "Core field name",
                "description": "Core field text.",
                "selector": {"text": None},
            }
        },
    },
    "dynamic": {"description": "Dynamic text for {who}."},
    "untitled": {"fields": {"some_key": {"selector": {"text": None}}}},
    "nulls": {
        "description": None,
        "fields": {"field_1": {"description": None, "selector": {"text": None}}},
    },
}

TRANSLATIONS = {
    "services": {
        "translated": {
            "name": "Translated name for {who}",
            "description": "Translated description for {who}.",
            "fields": {
                "duration": {"name": "Duration", "description": "How long it runs."},
                "mode": {"name": "Mode", "description": "Pick a mode."},
                "nested": {"name": "Nested"},
                "example_translated": {
                    "name": "Example field",
                    "description": "Has a translated example.",
                    "example": "Example: {who}",
                },
                "literal": {"description": LITERAL_BRACES},
            },
            "sections": {
                "section": {"name": "Section", "description": "Section description."}
            },
        }
    }
}


async def _no_op(call: ServiceCall) -> None:
    return None


@pytest.fixture
async def catalog_integration(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set up ``service_catalog`` with its services.yaml and translations.

    Core reads a mocked integration's files from its relative package path, so
    they live under the test's own cwd. JSON is valid YAML.
    """
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "homeassistant" / "components" / DOMAIN
    (folder / "translations").mkdir(parents=True)
    (folder / "services.yaml").write_text(json.dumps(SERVICES_YAML), encoding="utf-8")
    (folder / "translations" / "en.json").write_text(
        json.dumps(TRANSLATIONS), encoding="utf-8"
    )
    mock_integration(
        hass, MockModule(DOMAIN), top_level_files={"services.yaml", "translations"}
    )
    assert await async_setup_component(hass, DOMAIN, {})
    hass.services.async_register(
        DOMAIN,
        "translated",
        _no_op,
        supports_response=SupportsResponse.ONLY,
        description_placeholders=PLACEHOLDERS,
    )
    hass.services.async_register(
        DOMAIN, "dynamic", _no_op, description_placeholders=PLACEHOLDERS
    )
    for service in ("untranslated", "untitled", "nulls"):
        hass.services.async_register(DOMAIN, service, _no_op)
    # A services.yaml filter names its features as enum paths, and Core imports
    # every base component to resolve them, conversation among them, whose
    # requirements are not installed here. A runtime description (as script
    # fields are) carries the resolved values instead.
    hass.services.async_register(DOMAIN, "filtered", _no_op)
    async_set_service_schema(
        hass,
        DOMAIN,
        "filtered",
        {"fields": {"duration": {"filter": {"supported_features": [32]}}}},
    )


@pytest.fixture
def list_services(
    catalog_integration: None, component_core_client: HomeAssistantClient
) -> Any:
    """``ha_list_services`` served by the component's catalog command."""
    return ServiceDiscoveryTools(component_core_client).ha_list_services


async def _service(list_services: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = await list_services(domain=DOMAIN, detail_level="full", **arguments)
    service: dict[str, Any] = result["services"][f"{DOMAIN}.{name}"]
    return service


async def _core_field(
    hass: HomeAssistant, field: str, service: str = "translated"
) -> dict[str, Any]:
    description: dict[str, Any] = (await async_get_all_descriptions(hass))[DOMAIN][
        service
    ]["fields"][field]
    return description


async def test_translated_text_wins_and_placeholders_resolve(
    list_services: Any,
) -> None:
    service = await _service(list_services, "translated")
    fields = service["fields"]

    assert service["name"] == "Translated name for you"
    assert service["description"] == "Translated description for you."
    assert fields["duration"]["name"] == "Duration"
    assert fields["duration"]["description"] == "How long it runs."


async def test_section_keeps_its_name_collapsed_flag_and_nested_fields(
    list_services: Any,
) -> None:
    section = (await _service(list_services, "translated"))["fields"]["section"]

    assert section["name"] == "Section"
    assert section["description"] == "Section description."
    assert section["collapsed"] is True
    assert section["fields"]["nested"]["name"] == "Nested"


async def test_section_without_collapsed_keeps_only_what_core_sent(
    list_services: Any,
) -> None:
    section = (await _service(list_services, "translated"))["fields"]["plain_section"]

    assert "collapsed" not in section
    assert "nested" in section["fields"]


async def test_field_rules_are_passed_through_not_reinterpreted(
    hass: HomeAssistant, list_services: Any
) -> None:
    fields = (await _service(list_services, "translated"))["fields"]
    filtered = (await _service(list_services, "filtered"))["fields"]["duration"]

    core_filtered = await _core_field(hass, "duration", "filtered")
    assert filtered["filter"] == core_filtered["filter"]
    assert fields["duration"]["advanced"] is False
    assert fields["duration"]["default"] is False
    assert "required" not in fields["duration"]
    assert fields["mode"]["required"] is True


async def test_select_options_are_not_flattened_or_truncated(
    hass: HomeAssistant, list_services: Any
) -> None:
    fields = (await _service(list_services, "translated"))["fields"]

    assert fields["mode"]["selector"] == (await _core_field(hass, "mode"))["selector"]
    options = fields["mode"]["selector"]["select"]["options"]
    assert len(options) == len(SELECT_OPTIONS)
    assert options[0] == {"value": "red", "label": "Red"}


async def test_no_derived_type_or_synthetic_example(list_services: Any) -> None:
    fields = (await _service(list_services, "translated"))["fields"]

    for field in fields.values():
        assert "type" not in field
    assert "example" not in fields["duration"]


async def test_translated_example_wins_and_resolves_placeholders(
    list_services: Any,
) -> None:
    fields = (await _service(list_services, "translated"))["fields"]

    assert fields["example_translated"]["example"] == "Example: you"
    assert fields["example_core"]["example"] == "2020-01-01"
    assert "example" not in fields["example_none"]


async def test_response_survives_summary_and_full_but_fields_do_not(
    list_services: Any,
) -> None:
    full = await _service(list_services, "translated")
    summary = (await list_services(domain=DOMAIN, detail_level="summary"))["services"][
        f"{DOMAIN}.translated"
    ]

    assert full["response"] == {"optional": False}
    assert summary["response"] == {"optional": False}
    assert "fields" not in summary


async def test_untranslated_service_keeps_core_name_and_description(
    list_services: Any,
) -> None:
    service = await _service(list_services, "untranslated")

    assert service["name"] == "Core name"
    assert service["description"] == "Core description."


async def test_field_text_falls_back_to_core_payload(list_services: Any) -> None:
    field = (await _service(list_services, "untranslated"))["fields"]["core_field"]

    assert field["name"] == "Core field name"
    assert field["description"] == "Core field text."


async def test_title_case_is_the_last_resort(list_services: Any) -> None:
    service = await _service(list_services, "untitled")
    field = service["fields"]["some_key"]

    assert service["name"] == "Untitled"
    assert field["name"] == "Some Key"
    assert field["description"] == ""


async def test_query_matches_translated_text(list_services: Any) -> None:
    result = await list_services(query="translated description")

    assert f"{DOMAIN}.translated" in result["services"]
    assert f"{DOMAIN}.untranslated" not in result["services"]


async def test_unknown_placeholders_and_literal_braces_survive(
    list_services: Any,
) -> None:
    field = (await _service(list_services, "translated"))["fields"]["literal"]

    assert field["description"] == (
        'Runs for you; schema {"a": 1}; missing {other_placeholder}.'
    )


async def test_service_level_placeholders_resolve_in_core_text(
    list_services: Any,
) -> None:
    service = await _service(list_services, "dynamic")

    assert service["description"] == "Dynamic text for you."


async def test_null_core_description_normalizes_to_empty_string(
    list_services: Any,
) -> None:
    # An empty ``description:`` in services.yaml reaches the catalog as null.
    service = await _service(list_services, "nulls")

    assert service["description"] == ""
    assert service["fields"]["field_1"]["description"] == ""


@pytest.fixture
def drifted_list_services(hass: HomeAssistant, list_services: Any) -> Any:
    """Services whose runtime description carries non-mapping field metadata.

    ``async_set_service_schema`` stores the fields as given, unvalidated.
    """
    for service, fields in (
        ("not_a_mapping", "not-a-mapping"),
        ("mixed", {"good": {"selector": {}}, "bad": "not-a-mapping"}),
    ):
        hass.services.async_register(DRIFT_DOMAIN, service, _no_op)
        async_set_service_schema(hass, DRIFT_DOMAIN, service, {"fields": fields})
    return list_services


async def test_drifted_field_payloads_are_skipped_not_fatal(
    drifted_list_services: Any,
) -> None:
    """A shape drift in one field must not take the whole catalog down."""
    result = await drifted_list_services(domain=DRIFT_DOMAIN, detail_level="full")

    assert result["services"][f"{DRIFT_DOMAIN}.not_a_mapping"]["fields"] == {}
    assert set(result["services"][f"{DRIFT_DOMAIN}.mixed"]["fields"]) == {"good"}


async def test_discarded_field_metadata_is_labelled_incomplete(
    drifted_list_services: Any,
) -> None:
    """Dropping non-mapping field metadata must be reported, not swallowed."""
    result = await drifted_list_services(domain=DRIFT_DOMAIN, detail_level="full")

    assert len(result["warnings"]) == 1
    assert "incomplete" in result["warnings"][0]
    assert f"{DRIFT_DOMAIN}.not_a_mapping.fields" in result["warnings"][0]
    assert f"{DRIFT_DOMAIN}.mixed.fields.bad" in result["warnings"][0]


async def test_shaped_field_metadata_adds_no_incomplete_warning(
    list_services: Any,
) -> None:
    result = await list_services(domain=DOMAIN, detail_level="full")

    assert result["services"]
    assert "warnings" not in result


@pytest.fixture
def legacy_client(
    hass: HomeAssistant, catalog_integration: None, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` without the component, so the REST + translations path runs.

    The frontend integration cannot be set up here (its web assets are not
    installed), so only Core's own translations command is registered.
    """
    websocket_api.async_register_command(hass, websocket_get_translations)
    return core_client


def _translation_requests(sent: AsyncMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0].get("type") == "frontend/get_translations"
    ]


async def test_legacy_translations_request_uses_configured_language(
    hass: HomeAssistant, legacy_client: HomeAssistantClient
) -> None:
    await hass.config.async_update(language="de")

    with sent_messages(legacy_client) as sent:
        result = await ServiceDiscoveryTools(legacy_client).ha_list_services(
            domain=DOMAIN
        )

    assert _translation_requests(sent) == [
        {"type": "frontend/get_translations", "language": "de", "category": "services"}
    ]
    assert "warnings" not in result


async def test_unreadable_config_uses_english_and_warns(
    legacy_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(
            legacy_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("config unavailable")),
        ),
        sent_messages(legacy_client) as sent,
        caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_services"),
    ):
        result = await ServiceDiscoveryTools(legacy_client).ha_list_services(
            domain=DOMAIN
        )

    assert [request["language"] for request in _translation_requests(sent)] == ["en"]
    assert result["services"][f"{DOMAIN}.translated"]["name"] == (
        "Translated name for you"
    )
    assert any(
        "service descriptions may be untranslated: language unavailable" in warning
        for warning in result["warnings"]
    )
    assert "Could not read the configured language" in caplog.text
