"""Card definitions read from Home Assistant's frontend (#2632): the pure parts.

The engine itself runs Home Assistant's frontend code; the E2E suite exercises
it against a real frontend. These cover how the component finds that code and
turns its verdicts into warnings.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import dashboard_card_describe as describe_mod
from ha_mcp.tools.component_api import ComponentCaps

# The sibling module installs the homeassistant.* stubs the component needs.
from .test_component_ws_search import wsapi  # noqa: F401

cd = importlib.import_module("custom_components.ha_mcp_tools.card_definitions")


def _definitions(card_types: set[str], struct_keys: dict[str, Any]) -> Any:
    """An instance with no frontend: no card type has a validator to run."""
    definitions = object.__new__(cd.CardDefinitions)
    definitions._card_types = card_types
    definitions._struct_keys = struct_keys
    return definitions


def test_card_positions_cover_stacks_sections_and_conditional_cards() -> None:
    config = {
        "views": [
            {
                "cards": [
                    {"type": "vertical-stack", "cards": [{"type": "tile"}]},
                    {"type": "conditional", "card": {"type": "button"}},
                    {"type": "entity-filter", "card": {"title": "rows"}},
                ],
                "sections": [{"cards": [{"type": "heading"}]}, "not a section"],
            },
            {"strategy": {"type": "original-states"}},
        ]
    }
    assert [path for path, _ in cd._cards(config)] == [
        "views[0].cards[0]",
        "views[0].cards[0].cards[0]",
        "views[0].cards[1]",
        "views[0].cards[1].card",
        "views[0].cards[2]",
        "views[0].sections[0].cards[0]",
    ]


def test_missing_and_unknown_types_are_flagged_and_custom_cards_skipped() -> None:
    definitions = _definitions({"tile"}, {"tile": None})
    cards = [{"entity": "light.x"}, {"type": "tyle"}, {"type": "custom:x-card"}]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert warnings == [
        "views[0].cards[0]: no card type configured",
        "views[0].cards[1]: unknown card type 'tyle'",
    ]


def test_warnings_are_capped() -> None:
    definitions = _definitions(set(), {})
    cards = [{"type": f"t{i}"} for i in range(25)]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert len(warnings) == 21
    assert warnings[-1] == "...and 5 more card problems"


def test_unknown_key_names_the_closest_card_option() -> None:
    definitions = _definitions({"tile"}, {"tile": ["type", "entity", "color"]})
    never = {"type": "never", "message": "Expected a value of type `never`"}

    assert (
        definitions._explain("tile", {**never, "path": ["colour"]})
        == "'colour' is not a tile card option; did you mean 'color'?"
    )
    assert definitions._explain("tile", {**never, "path": ["card_mod"]}) is None
    assert (
        definitions._explain(
            "tile",
            {
                "type": "enums",
                "path": ["tap_action", "action"],
                "message": 'At path: tap_action.action -- Expected one of `"none"`',
            },
        )
        == 'tap_action.action: Expected one of `"none"`'
    )


def test_editor_registration_is_found_and_imports_resolve() -> None:
    chunk = (
        'V=(0,o.Cg)([(0,s.EM)("hui-tile-card-editor")],V);'
        'e=document.createElement("hui-map-card-editor")'
    )
    assert [m.group(1) for m in cd._TAG_RE.finditer(chunk)] == ["hui-tile-card-editor"]
    imports = "c=a(97400),_=(a(47551),a(20541)),m=a(80140)"
    assert dict(cd._ALIAS_RE.findall(imports)) == {
        "c": "97400",
        "_": "20541",
        "m": "80140",
    }


def test_local_definition_stops_at_the_top_level_comma() -> None:
    body = 'var x=(0,l.Ik)({a:(0,l.vP)(["b,c",`d,${1}`]),e:f}),y=2;class V{}'

    assert cd._local_definition(body, "x") == (
        '(0,l.Ik)({a:(0,l.vP)(["b,c",`d,${1}`]),e:f})'
    )
    assert cd._local_definition(body, "y") == "2"
    assert cd._local_definition(body, "z") is None


def test_editor_forms_are_found_as_functions_or_constants() -> None:
    body = (
        'this._schema=(0,c.A)(e=>[{name:"entity"}]);'
        'C=[{name:"title"}],x=1;render(){return `<ha-form .schema=${C}>`}'
    )
    assert cd._schema_expressions(body) == ['e=>[{name:"entity"}]', '[{name:"title"}]']


@pytest.mark.asyncio
async def test_card_warnings_never_fail_a_write(monkeypatch) -> None:
    monkeypatch.setattr(
        cd, "async_get_definitions", AsyncMock(side_effect=RuntimeError("boom"))
    )
    assert await cd.async_card_warnings(MagicMock(), {"views": []}) == []


# --- server: ha_config_get_dashboard(describe=True) ------------------------


def _caps(*names: str) -> ComponentCaps:
    return ComponentCaps(1, "2.2.2", frozenset(names), {})


@pytest.fixture
def component(monkeypatch) -> AsyncMock:
    ws = AsyncMock()
    monkeypatch.setattr(
        describe_mod,
        "get_component_caps",
        AsyncMock(return_value=_caps("dashboard_cards")),
    )
    monkeypatch.setattr(
        describe_mod, "get_websocket_client", AsyncMock(return_value=ws)
    )
    return ws


@pytest.mark.asyncio
async def test_describe_compacts_the_editor_form(component: AsyncMock) -> None:
    component.send_command.return_value = {
        "result": {
            "success": True,
            "name": "Tile",
            "description": "An entity at a glance.",
            "fields": [
                {"name": "entity", "selector": {"entity": {}}},
                {
                    "name": "content",
                    "type": "expandable",
                    "flatten": True,
                    "schema": [
                        {
                            "name": "",
                            "type": "grid",
                            "schema": [
                                {
                                    "name": "color",
                                    "selector": {"ui_color": {}},
                                    "description": "Inactive state is not colored.",
                                },
                            ],
                        },
                        {"name": "", "type": "divider"},
                    ],
                },
            ],
        }
    }
    client = MagicMock(base_url="http://ha", token="t")

    result = await describe_mod.describe_card_response(client, "tile")

    component.send_command.assert_awaited_once_with(
        describe_mod.WS_DASHBOARD_CARDS, card_type="tile"
    )
    assert result["fields"] == [
        {"name": "entity", "type": "entity"},
        {
            "name": "content",
            "type": "section",
            "fields": [
                {
                    "name": "color",
                    "type": "ui_color",
                    "description": "Inactive state is not colored.",
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_describe_unknown_card_type_lists_the_real_ones(
    component: AsyncMock,
) -> None:
    component.send_command.return_value = {
        "result": {
            "success": False,
            "error": "unknown_card_type",
            "card_types": ["tile", "grid"],
        }
    }
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(MagicMock(), "tyle")
    assert "VALIDATION_INVALID_PARAMETER" in str(err.value)
    assert "tile, grid" in str(err.value)


@pytest.mark.asyncio
async def test_describe_without_the_component_says_so(monkeypatch) -> None:
    monkeypatch.setattr(
        describe_mod, "get_component_caps", AsyncMock(return_value=_caps("search"))
    )
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(MagicMock(), "tile")
    assert "COMPONENT_NOT_INSTALLED" in str(err.value)
