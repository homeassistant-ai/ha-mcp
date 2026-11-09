"""Service-catalog shaping: the server's own translation, section and fallback rules.

The inputs here are deliberately small and hand-written to exercise the server's
logic; they are **not** Home Assistant's catalog and must not be presented as
Core data. Core's real shape (translated service/field/section text,
description placeholders, a translated field example, a collapsed section and
``response``) is asserted against a live Core image in
``tests/src/e2e/workflows/services/test_list_services.py``, where the pinned
image and the seeded ``kitchen_sink`` integration provide it.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import component_api
from ha_mcp.tools.tools_services import _process_services

from .test_ha_list_services_component_routing import _build_list_services

_SELECT_OPTIONS = ["red", "green", "blue", "yellow", "orange", "purple"]


def _catalog() -> list[dict[str, Any]]:
    """A synthetic catalog shaped like Core's payload, not a copy of it."""
    return [
        {
            "domain": "demo",
            "services": {
                "translated": {
                    "fields": {
                        "duration": {
                            "filter": {"supported_features": [32]},
                            "selector": {
                                "number": {
                                    "min": 0,
                                    "max": 300,
                                    "unit_of_measurement": "seconds",
                                }
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
                                        for value in _SELECT_OPTIONS
                                    ]
                                }
                            },
                        },
                        "section": {
                            "collapsed": True,
                            "fields": {"nested": {"selector": {"text": {}}}},
                        },
                        "plain_section": {
                            "fields": {"nested": {"selector": {"text": {}}}},
                        },
                        "example_translated": {
                            "example": "[1, 1]",
                            "selector": {"object": {"multiple": False}},
                        },
                        "example_core": {
                            "example": "2020-01-01",
                            "selector": {"date": {}},
                        },
                        "example_none": {"example": None, "selector": {"date": {}}},
                    },
                    "description_placeholders": {"who": "you"},
                    "response": {"optional": False},
                },
                "untranslated": {
                    "name": "Core name",
                    "description": "Core description.",
                    "fields": {
                        "core_field": {
                            "name": "Core field name",
                            "description": "Core field text.",
                        }
                    },
                },
                "dynamic": {
                    "description": "Dynamic text for {who}.",
                    "description_placeholders": {"who": "you"},
                },
                "untitled": {"fields": {"some_key": {"selector": {"text": {}}}}},
            },
        },
        {"domain": "other", "services": {"thing": {"fields": {}}}},
    ]


def _translations() -> dict[str, str]:
    return {
        "component.demo.services.translated.name": "Translated name for {who}",
        "component.demo.services.translated.description": (
            "Translated description for {who}."
        ),
        "component.demo.services.translated.fields.duration.name": "Duration",
        "component.demo.services.translated.fields.duration.description": (
            "How long it runs."
        ),
        "component.demo.services.translated.fields.mode.name": "Mode",
        "component.demo.services.translated.fields.mode.description": "Pick a mode.",
        "component.demo.services.translated.sections.section.name": "Section",
        "component.demo.services.translated.sections.section.description": (
            "Section description."
        ),
        "component.demo.services.translated.fields.nested.name": "Nested",
        "component.demo.services.translated.fields.example_translated.name": (
            "Example field"
        ),
        "component.demo.services.translated.fields.example_translated.description": (
            "Has a translated example."
        ),
        "component.demo.services.translated.fields.example_translated.example": (
            "Example: {who}"
        ),
    }


def _process(
    *,
    domain: str | None = None,
    query: str | None = None,
    detail_level: str = "full",
    translations: dict[str, str] | None = None,
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _process_services(
        rest_services=_catalog() if catalog is None else catalog,
        translations=_translations() if translations is None else translations,
        domain_filter=domain,
        query_filter=query,
        limit=200,
        offset=0,
        detail_level=detail_level,  # type: ignore[arg-type]
    )


def _service(result: dict[str, Any], key: str) -> dict[str, Any]:
    return result["services"][key]


def test_translated_text_wins_and_placeholders_resolve() -> None:
    service = _service(_process(domain="demo"), "demo.translated")
    fields = service["fields"]

    assert service["name"] == "Translated name for you"
    assert service["description"] == "Translated description for you."
    assert fields["duration"]["name"] == "Duration"
    assert fields["duration"]["description"] == "How long it runs."


def test_section_keeps_its_name_collapsed_flag_and_nested_fields() -> None:
    fields = _service(_process(domain="demo"), "demo.translated")["fields"]
    section = fields["section"]

    assert section["name"] == "Section"
    assert section["collapsed"] is True
    assert section["fields"]["nested"]["name"] == "Nested"


def test_section_without_collapsed_keeps_only_what_core_sent() -> None:
    fields = _service(_process(domain="demo"), "demo.translated")["fields"]
    section = fields["plain_section"]

    assert "collapsed" not in section
    assert "nested" in section["fields"]


def test_field_rules_are_passed_through_not_reinterpreted() -> None:
    fields = _service(_process(domain="demo"), "demo.translated")["fields"]

    assert fields["duration"]["filter"] == {"supported_features": [32]}
    assert fields["duration"]["advanced"] is False
    assert fields["duration"]["default"] is False
    assert "required" not in fields["duration"]
    assert fields["mode"]["required"] is True


def test_select_options_are_not_flattened_or_truncated() -> None:
    options = _service(_process(domain="demo"), "demo.translated")["fields"]["mode"][
        "selector"
    ]["select"]["options"]

    assert len(options) == len(_SELECT_OPTIONS)
    assert options[0] == {"value": "red", "label": "Red"}


def test_no_derived_type_or_synthetic_example() -> None:
    fields = _service(_process(domain="demo"), "demo.translated")["fields"]

    for field in fields.values():
        assert "type" not in field
    assert "example" not in fields["duration"]


def test_translated_example_wins_and_resolves_placeholders() -> None:
    fields = _service(_process(domain="demo"), "demo.translated")["fields"]

    assert fields["example_translated"]["example"] == "Example: you"
    assert fields["example_core"]["example"] == "2020-01-01"
    assert "example" not in fields["example_none"]


def test_response_survives_summary_and_full_but_fields_do_not() -> None:
    full = _service(_process(domain="demo"), "demo.translated")
    summary = _service(
        _process(domain="demo", detail_level="summary"), "demo.translated"
    )

    assert full["response"] == {"optional": False}
    assert summary["response"] == {"optional": False}
    assert "fields" not in summary


def test_untranslated_service_keeps_core_name_and_description() -> None:
    service = _service(_process(domain="demo"), "demo.untranslated")

    assert service["name"] == "Core name"
    assert service["description"] == "Core description."


def test_field_text_falls_back_to_core_payload() -> None:
    field = _service(_process(domain="demo"), "demo.untranslated")["fields"][
        "core_field"
    ]

    assert field["name"] == "Core field name"
    assert field["description"] == "Core field text."


def test_title_case_is_the_last_resort() -> None:
    field = _service(_process(domain="demo"), "demo.untitled")["fields"]["some_key"]

    assert field["name"] == "Some Key"
    assert field["description"] == ""


def test_query_matches_translated_text() -> None:
    result = _process(query="translated description")

    assert "demo.translated" in result["services"]
    assert "demo.untranslated" not in result["services"]


def test_unknown_placeholders_and_literal_braces_survive() -> None:
    translations = copy.deepcopy(_translations())
    translations["component.demo.services.translated.fields.duration.description"] = (
        'Runs for {who}; schema {"a": 1}; missing {other_placeholder}.'
    )

    description = _service(
        _process(domain="demo", translations=translations), "demo.translated"
    )["fields"]["duration"]["description"]

    assert description == 'Runs for you; schema {"a": 1}; missing {other_placeholder}.'


def test_service_level_placeholders_resolve_in_core_text() -> None:
    service = _service(_process(domain="demo"), "demo.dynamic")

    assert service["description"] == "Dynamic text for you."


def test_null_core_description_normalizes_to_empty_string() -> None:
    catalog = [
        {
            "domain": "demo",
            "services": {
                "nulls": {
                    "description": None,
                    "fields": {"field_1": {"description": None, "selector": {}}},
                }
            },
        }
    ]

    service = _service(_process(catalog=catalog, translations={}), "demo.nulls")

    assert service["description"] == ""
    assert service["fields"]["field_1"]["description"] == ""


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

    result = _process(catalog=catalog, translations={})

    assert result["services"]["drift.not_a_mapping"]["fields"] == {}
    assert set(result["services"]["drift.mixed"]["fields"]) == {"good"}


def test_discarded_field_metadata_is_labelled_incomplete() -> None:
    """Dropping non-mapping field metadata must be reported, not swallowed."""
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
    warnings: list[str] = []

    _process_services(
        rest_services=catalog,
        translations={},
        limit=200,
        detail_level="full",
        warnings=warnings,
    )

    assert len(warnings) == 1
    assert "incomplete" in warnings[0]
    assert "drift.not_a_mapping.fields" in warnings[0]
    assert "drift.mixed.fields.bad" in warnings[0]


def test_shaped_field_metadata_adds_no_incomplete_warning() -> None:
    warnings: list[str] = []

    _process_services(
        rest_services=_catalog(),
        translations=_translations(),
        limit=200,
        detail_level="full",
        warnings=warnings,
    )

    assert warnings == []


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
        return _catalog()

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.translation_messages.append(dict(msg))
        return {"success": True, "result": {"resources": _translations()}}


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
async def test_unreadable_config_uses_english_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _LegacyClient(raise_on_config=True)

    with caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_services"):
        result = await _build_list_services(client)()

    assert client.translation_messages[0]["language"] == "en"
    assert any(
        "service descriptions may be untranslated: language unavailable" in warning
        for warning in result.get("warnings", [])
    )
    assert "Could not read the configured language" in caplog.text


@pytest.mark.asyncio
async def test_config_without_a_language_uses_english_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _LegacyClient(config={"time_zone": "UTC"})

    with caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_services"):
        result = await _build_list_services(client)()

    assert client.translation_messages[0]["language"] == "en"
    assert any(
        "service descriptions may be untranslated: language unavailable" in warning
        for warning in result.get("warnings", [])
    )
    assert "no usable language" in caplog.text
