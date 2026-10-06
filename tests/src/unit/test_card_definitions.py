"""Card definitions read from Home Assistant's frontend (#2632): the pure parts.

The engine itself runs Home Assistant's frontend code; the E2E suite exercises
it against a real frontend. These cover how the component finds that code and
turns its verdicts into warnings.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import dashboard_card_describe as describe_mod
from ha_mcp.tools.component_api import ComponentCaps

# The sibling module installs the homeassistant.* stubs the component needs.
importlib.import_module(".test_component_ws_search", __package__)

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


def test_missing_and_unknown_types_are_flagged() -> None:
    definitions = _definitions({"tile"}, {"tile": None})
    cards = [{"entity": "light.x"}, {"type": "tyle"}, {"type": "custom:x-card"}]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert warnings == [
        "views[0].cards[0]: no card type configured",
        "views[0].cards[1]: unknown card type 'tyle'",
    ]


def test_header_and_custom_children_exclude_non_card_metadata() -> None:
    config = {
        "views": [
            {
                "header": {"card": {"type": "markdown"}},
                "cards": [
                    {
                        "type": "custom:button-card",
                        "custom_fields": {
                            "label": "plain text",
                            "nested": {
                                "card": {
                                    "type": "custom:state-switch",
                                    "states": {
                                        "on": {
                                            "type": "tile",
                                            "features": [{"type": "light-brightness"}],
                                        },
                                    },
                                }
                            },
                        },
                    }
                ],
            }
        ]
    }
    cards = cd._cards(config)
    assert [card["type"] for _, card in cards] == [
        "markdown",
        "custom:button-card",
        "custom:state-switch",
        "tile",
    ]
    assert cards[0][0] == "views[0].header.card"
    assert cards[-1][0] == 'views[0].cards[0].custom_fields["nested"].card.states["on"]'


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


def test_entrypoint_dependencies_load_without_browser_startup(tmp_path) -> None:
    """HA 2026.10 card structs import core-js helpers held in core/app bundles."""
    frontend = tmp_path / "frontend_latest"
    frontend.mkdir()
    (frontend / "core.js").write_text(
        'var e,t,r={7(e,t,r){r.d(t,{},{field:"entity"})}},cache={};'
        'function start(){throw new Error("browser startup must not run")}'
        "var url=import.meta.url;start();",
        encoding="utf-8",
    )
    (frontend / "cards.js").write_text(
        'export const __webpack_modules__={8(e){(0,e.EM)("hui-tile-card")},'
        '9(e){(0,e.EM)("hui-entities-card")}};',
        encoding="utf-8",
    )
    definitions = cd.CardDefinitions(tmp_path)
    result = definitions._evaluate("schema", "var x=r(7);", "[{name:x.field}]")
    assert result == [{"name": "entity"}]


def test_failed_module_is_not_reused_as_partial_exports(tmp_path) -> None:
    frontend = tmp_path / "frontend_latest"
    frontend.mkdir()
    (frontend / "chunk.js").write_text(
        'export const __webpack_modules__={7(e,t){t.field="partial";'
        'throw new Error("dependency unavailable")}};',
        encoding="utf-8",
    )
    (frontend / "cards.js").write_text(
        'export const __webpack_modules__={8(e){(0,e.EM)("hui-tile-card")},'
        '9(e){(0,e.EM)("hui-entities-card")}};',
        encoding="utf-8",
    )
    definitions = cd.CardDefinitions(tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError, match="require-failed"):
            definitions._evaluate("struct", "var x=r(7);", "x.field")


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
            "name": "color",
            "type": "ui_color",
            "description": "Inactive state is not colored.",
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
    assert "not a known card type" in str(err.value)


@pytest.mark.asyncio
async def test_describe_without_the_component_says_so(monkeypatch) -> None:
    monkeypatch.setattr(
        describe_mod, "get_component_caps", AsyncMock(return_value=_caps("search"))
    )
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(MagicMock(), "tile")
    assert "COMPONENT_NOT_INSTALLED" in str(err.value)


# --- custom cards ------------------------------------------------------------

cc = importlib.import_module("custom_components.ha_mcp_tools.custom_cards")


def test_resource_urls_map_to_files_inside_www(tmp_path) -> None:
    www = tmp_path / "www"
    assert cc.resource_path(tmp_path, "/hacsfiles/mushroom/mushroom.js?hacstag=1") == (
        (www / "community" / "mushroom" / "mushroom.js").resolve()
    )
    assert cc.resource_path(tmp_path, "/local/cards/my%20card.js") == (
        (www / "cards" / "my card.js").resolve()
    )
    for url in (
        "/local/../secrets.yaml",
        "/hacsfiles/../../configuration.js",
        "/local/styles.css",
        "https://cdn.example.com/card.js",
    ):
        assert cc.resource_path(tmp_path, url) is None, url


def _tarball(worker: str) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = worker.encode()
        info = tarfile.TarInfo("package/worker.js")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_linkedom_is_checked_and_made_a_scoped_script(monkeypatch) -> None:
    import base64
    import hashlib

    tarball = _tarball(
        "export const shared = 1;\n"
        "function parseHTML() {}\nclass GlobalEvent {}\n"
        "export { parseHTML, GlobalEvent as Event };\n"
    )
    digest = base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    monkeypatch.setattr(cc, "LINKEDOM_INTEGRITY", f"sha512-{digest}")

    script = cc.dom_script(tarball)

    assert script.startswith("(function () {\nconst shared = 1;")
    assert script.endswith(
        "globalThis.__linkedom = {parseHTML: parseHTML, Event: GlobalEvent};\n})();"
    )
    monkeypatch.setattr(cc, "LINKEDOM_INTEGRITY", "sha512-other")
    with pytest.raises(ValueError, match="integrity"):
        cc.dom_script(tarball)


def test_custom_card_messages_read_plainly() -> None:
    never = "At path: colour -- Expected a value of type `never`, but received: `1`"
    assert (
        cd._explain_message("my-card", never) == "'colour' is not a my-card card option"
    )
    assert cd._explain_message("my-card", never.replace("colour", "card_mod")) is None
    assert (
        cd._explain_message("my-card", "At path: size -- Expected a number")
        == "size: Expected a number"
    )
    assert cd._explain_message("my-card", "value.series is missing") == (
        "value.series is missing"
    )


def test_custom_cards_report_their_own_problems_and_unknown_ones_none() -> None:
    answers = {"good-card": [], "picky-card": ["picky-card needs an entity"]}
    custom = MagicMock()
    custom.check.side_effect = lambda tag, card: answers.get(tag)
    definitions = _definitions(set(), {})
    cards = [
        {"type": "custom:good-card"},
        {"type": "custom:picky-card"},
        {"type": "custom:not-installed-card"},
    ]

    warnings = definitions.validate({"views": [{"cards": cards}]}, custom)

    assert warnings == [
        "views[0].cards[1] (custom:picky-card): picky-card needs an entity"
    ]


def test_removed_resources_are_unloaded(tmp_path, monkeypatch) -> None:
    card_file = tmp_path / "a-card.js"
    card_file.write_text("// card")
    custom = cc.CustomCards("dom")
    monkeypatch.setattr(
        custom, "_load", lambda path, size: MagicMock(tags=["a-card"], memory=0)
    )

    custom.refresh([card_file])
    assert custom.check("a-card", {"type": "custom:a-card"}) is not None
    custom.refresh([])
    assert custom.check("a-card", {"type": "custom:a-card"}) is None


def test_deleted_resource_files_are_evicted(tmp_path, monkeypatch) -> None:
    path = tmp_path / "deleted.js"
    path.write_text("// card")
    custom = cc.CustomCards("dom")
    monkeypatch.setattr(
        cc, "_Bundle", MagicMock(return_value=MagicMock(tags=["a-card"]))
    )
    custom.refresh([path])
    assert custom._owner("a-card") is not None
    path.unlink()
    custom.refresh([path])  # The resource registration still exists.
    assert custom._owner("a-card") is None


def test_capacity_skipped_resources_retry_without_file_changes(
    tmp_path, monkeypatch
) -> None:
    files = [tmp_path / f"card-{i}.js" for i in range(2)]
    for path in files:
        path.write_text("// card")
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", cc._BUNDLE_MEMORY)
    factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(cc, "_Bundle", factory)
    custom = cc.CustomCards("dom")
    custom.refresh(files)
    assert custom._bundles[files[1]][1] is None
    custom.refresh(files[1:])
    assert custom._bundles[files[1]][1] is not None
    assert factory.call_count == 2


def test_failed_resources_stay_cached_but_type_changes_reload(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "broken.js"
    path.write_text("invalid code")
    factory = MagicMock(side_effect=ValueError("bad script"))
    monkeypatch.setattr(cc, "_Bundle", factory)
    custom = cc.CustomCards("dom")
    custom.refresh([path])
    custom.refresh([path])
    assert factory.call_count == 1  # Do not retry broken code on every request.
    custom.refresh([path], {path})
    assert factory.call_count == 2
    assert factory.call_args.kwargs["module"] is True
    assert factory.call_args.kwargs["deadline"] is not None


def test_oversized_card_files_are_not_run(tmp_path, monkeypatch) -> None:
    card_file = tmp_path / "huge-card.js"
    card_file.write_text("x" * 64)
    monkeypatch.setattr(cc, "_MAX_SOURCE_BYTES", 32)
    monkeypatch.setattr(cc, "_Bundle", MagicMock(side_effect=AssertionError("ran")))

    assert cc.CustomCards("dom")._load(card_file, card_file.stat().st_size) is None


@pytest.mark.asyncio
async def test_first_use_shares_one_background_index_build(monkeypatch):
    release = asyncio.Event()
    definitions = object()
    runtime = AsyncMock(return_value=True)

    async def build(fn):
        await release.wait()
        return definitions

    monkeypatch.setattr(cd, "_definitions", None)
    monkeypatch.setattr(cd, "_build_task", None)
    monkeypatch.setattr(cd, "async_ensure_runtime", runtime)
    hass = MagicMock()
    hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
    hass.async_add_executor_job = AsyncMock(side_effect=build)
    try:
        assert await cd.async_get_definitions(hass, timeout=0.01) is None
        assert await cd.async_get_definitions(hass, timeout=0.01) is None
        runtime.assert_awaited_once()
        release.set()
        assert await cd.async_get_definitions(hass, timeout=1) is definitions
        hass.async_add_executor_job.assert_awaited_once()
    finally:
        release.set()
        await cd._build_task


@pytest.mark.asyncio
async def test_card_validation_executor_cannot_hold_a_saved_write(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_CARD_WORK_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(
        cd, "async_get_definitions", AsyncMock(return_value=MagicMock())
    )
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda *args: None)

    async def stuck(*args):
        await asyncio.Event().wait()

    hass.async_add_executor_job.side_effect = stuck
    assert await asyncio.wait_for(cd.async_card_warnings(hass, {}), 0.5) == []


@pytest.mark.asyncio
async def test_slow_custom_refresh_preserves_builtin_describe(monkeypatch) -> None:
    import voluptuous as vol

    definitions = MagicMock()
    definitions.card_types.return_value = [{"type": "tile"}]
    monkeypatch.setattr(
        cd, "async_get_definitions", AsyncMock(return_value=definitions)
    )
    monkeypatch.setattr(cd, "_CUSTOM_WAIT_SECONDS", 0.01, raising=False)

    async def slow(hass, timeout=None):
        if timeout is None:
            await asyncio.Event().wait()

    monkeypatch.setattr(cd, "async_get_custom_cards", slow)
    prep = cd.command_specs(vol)[0][2]
    result = await asyncio.wait_for(prep(MagicMock(), {}), 0.5)
    assert result["result"]["success"] is True
    assert result["result"]["card_types"] == [{"type": "tile"}]


def test_bundle_admission_reserves_room_for_later_heap_growth(
    tmp_path, monkeypatch
) -> None:
    custom = cc.CustomCards("dom")
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", 100)
    monkeypatch.setattr(cc, "_BUNDLE_MEMORY", 40)
    custom._bundles = {tmp_path / str(i): (0, MagicMock(memory=1)) for i in range(2)}
    card_file = tmp_path / "new.js"
    card_file.write_text("// card")
    load = MagicMock()
    monkeypatch.setattr(cc, "_Bundle", load)
    assert custom._load(card_file, 7) is None
    load.assert_not_called()


@pytest.mark.asyncio
async def test_describe_drops_stale_capabilities_after_unknown_command(
    component, monkeypatch
) -> None:
    from ha_mcp.tools import component_api

    client = MagicMock()
    component_api._CAPS_CACHE[client] = _caps("dashboard_cards")
    error = RuntimeError("removed command")
    error.code = "unknown_command"
    component.send_command.side_effect = error
    with pytest.raises(ToolError):
        await describe_mod.describe_card_response(client, "tile")
    assert client not in component_api._CAPS_CACHE
