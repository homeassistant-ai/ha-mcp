"""Card definitions read from Home Assistant's frontend (#2632).

The component builds an index from the frontend Core serves, checks a saved
dashboard against it, and answers ``dashboard_cards`` with a card's editor
form. These run the index on a small frontend build written to disk, the
command and the describe tool on a real ``hass``, and custom cards through
their real QuickJS sandbox (the E2E suite runs the real frontend and DOM).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._card_definitions_core import (
    FrontendOnDisk,
    build_definitions,
    card_file,
    cc,
    cd,
    counted_full_collections,
    dcw,
    describe_mod,
)
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import frontend as frontend
from ._card_definitions_core import idle_custom_cards as idle_custom_cards
from ._card_definitions_core import stalled_card_download as stalled_card_download

__all__ = ["cc", "cd", "dcw", "describe_mod"]

# The editor form of ``tile``: a field, and one nested in an expandable grid.
_FORM_EDITOR = (
    "var s=r(6),v=(0,s.object)({type:s.string,entity:s.string,color:s.string});"
    "class C{constructor(){this._schema=(0,s.memo)(e=>["
    '{name:"entity",selector:{entity:{}}},'
    '{name:"content",type:"expandable",flatten:!0,schema:['
    '{name:"",type:"grid",schema:[{name:"color",selector:{ui_color:{}},'
    'description:"Inactive state is not colored."}]},'
    '{name:"",type:"divider"}]}])}'
    "setConfig(c){(0,s.assert)(c,v)}}"
)
# A dependency the editors import that cannot be loaded: it assigns an export,
# then throws.
_FAILING_MODULE = (
    'export const __webpack_modules__={7(e,t){t.field="partial";'
    'throw new Error("dependency unavailable")}};'
)
_FAILING_EDITOR = (
    "var x=r(7),s=r(6),v=(0,s.object)({type:s.string,entity:x.field});"
    'class C{constructor(){this._schema=(0,s.memo)(e=>[{name:"entity"}])}'
    "setConfig(c){(0,s.assert)(c,v)}}"
)
# Home Assistant 2026.10 card structs import helpers held in a core bundle,
# whose module registry sits beside startup code that needs a browser.
_CORE_BUNDLE = (
    'var e,t,r={7(e,t,r){r.d(t,{},{field:"entity"})}},cache={};'
    'function start(){throw new Error("browser startup must not run")}'
    "var url=import.meta.url;start();"
)
_ENTRYPOINT_EDITOR = (
    "var x=r(7),s=r(6),v=(0,s.object)({type:s.string,entity:s.string});"
    "class C{constructor(){this._schema=(0,s.memo)(e=>[{name:x.field}])}"
    "setConfig(c){(0,s.assert)(c,v)}}"
)
_PER_CARD = 16 * 1024 * 1024  # one sandbox's heap allowance in these tests


def _paths(warnings: list[str]) -> list[str]:
    return [warning.split(":")[0] for warning in warnings]


def test_every_card_position_is_checked_except_an_entity_filters_card(
    tmp_path: Path,
) -> None:
    """A card in a stack, a section or a conditional is checked; an
    entity-filter's ``card`` is not (its parent fills it in)."""
    definitions = build_definitions(tmp_path)
    config = {
        "views": [
            {
                "cards": [
                    {"type": "vertical-stack", "cards": [{"type": "stack-child"}]},
                    {"type": "conditional", "card": {"type": "button"}},
                    {"type": "entity-filter", "card": {"title": "rows"}},
                ],
                "sections": [{"cards": [{"type": "heading"}]}, "not a section"],
            },
            {"strategy": {"type": "original-states"}},
        ]
    }

    assert _paths(definitions.validate(config)) == [
        "views[0].cards[0]",
        "views[0].cards[0].cards[0]",
        "views[0].cards[1]",
        "views[0].cards[1].card",
        "views[0].cards[2]",
        "views[0].sections[0].cards[0]",
    ]


def test_a_card_without_a_type_or_with_a_misspelt_one_is_flagged(
    tmp_path: Path,
) -> None:
    definitions = build_definitions(tmp_path)
    cards = [
        {"entity": "light.x"},
        {"type": "tyle"},
        {"type": "custom:x-card"},
        {"type": "tile"},
    ]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert warnings == [
        "views[0].cards[0]: no card type configured",
        "views[0].cards[1]: unknown card type 'tyle'",
    ]


def test_header_and_a_custom_cards_children_are_checked_but_not_its_metadata(
    tmp_path: Path,
) -> None:
    definitions = build_definitions(tmp_path)
    config = {
        "views": [
            {
                "header": {"card": {"type": "markdown-x"}},
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
                                            "type": "tyle",
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

    assert definitions.validate(config) == [
        "views[0].header.card: unknown card type 'markdown-x'",
        'views[0].cards[0].custom_fields["nested"].card.states["on"]: '
        + "unknown card type 'tyle'",
    ]


def test_warnings_are_capped_and_say_how_many_were_left_out(tmp_path: Path) -> None:
    definitions = build_definitions(tmp_path)

    few = definitions.validate({"views": [{"cards": [{"type": "a"}, {"type": "b"}]}]})
    many = definitions.validate(
        {"views": [{"cards": [{"type": f"t{i}"} for i in range(25)]}]}
    )

    assert len(few) == 2
    left_out = 25 - (len(many) - 1)
    assert 0 < left_out < 25
    assert many[-1] == f"...and {left_out} more card diagnostics"


def test_an_option_the_editor_does_not_list_is_flagged_but_card_mod_is_not(
    tmp_path: Path,
) -> None:
    definitions = build_definitions(tmp_path)
    cards = [
        {"type": "tile", "colour": "red"},
        {"type": "tile", "card_mod": {"style": "x"}},
        {"type": "tile", "tap_action": {"action": "bad"}},
        {"type": "tile", "entity": "light.x"},
    ]
    advisory = "editor schema advisory; runtime support may differ"

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert warnings == [
        f"views[0].cards[0] (tile): {advisory}: "
        "'colour' is not listed in the tile editor schema",
        f"views[0].cards[2] (tile): {advisory}: "
        'tap_action.action: Expected one of `"none"`, but received: `"bad"`',
    ]


def test_a_card_named_only_by_createelement_gets_no_editor(tmp_path: Path) -> None:
    """``createElement("hui-map-card-editor")`` mentions the tag; it does not
    register it, so ``map`` must not borrow the module that mentions it."""
    tile_editor = (
        "var s=r(6),v=(0,s.object)({type:s.string,entity:s.string});"
        "class C{setConfig(c){(0,s.assert)(c,v)}}"
        ';var e=document.createElement("hui-map-card-editor")'
    )
    definitions = build_definitions(tmp_path, {"tile": tile_editor}, cards=("map",))

    tile = definitions.describe("tile")
    mapped = definitions.describe("map")

    assert tile is not None
    assert [field["name"] for field in tile["fields"]] == ["entity"]
    assert mapped is not None
    assert mapped["fields"] is None


def test_an_import_bound_to_a_comma_sequence_uses_its_last_module(
    tmp_path: Path,
) -> None:
    """``_=(a(47551),a(20541))`` binds ``_`` to module 20541."""
    editor = (
        "var c=r(97400),_=(r(47551),r(20541)),m=r(80140);"
        "class C{constructor(){this._schema=(0,m.A)(e=>[{name:_.field}])}}"
    )
    modules = (
        '20541(m,e,r){r.d(e,{},{field:"entity"})},'
        '47551(m,e,r){r.d(e,{},{field:"wrong"})}'
    )
    definitions = build_definitions(tmp_path, {"tile": editor}, modules=modules)

    described = definitions.describe("tile")

    assert described is not None
    assert [field["name"] for field in described["fields"]] == ["entity"]


def test_entrypoint_dependencies_load_without_browser_startup(tmp_path: Path) -> None:
    definitions = build_definitions(
        tmp_path, {"tile": _ENTRYPOINT_EDITOR}, files={"core.js": _CORE_BUNDLE}
    )

    described = definitions.describe("tile")

    assert described is not None
    assert described["fields"] == [{"name": "entity"}]


def test_a_module_that_failed_is_not_reused_as_partial_exports(tmp_path: Path) -> None:
    """The failing module is imported by two editors: neither may build its
    struct from the exports it had assigned before it threw."""
    definitions = build_definitions(
        tmp_path,
        {"tile": _FAILING_EDITOR, "map": _FAILING_EDITOR},
        cards=("map",),
        files={"chunk.js": _FAILING_MODULE},
    )

    coverage = {
        card: (definitions.describe(card) or {}).get("field_coverage")
        for card in ("tile", "map", "entities")
    }

    assert coverage == {
        "tile": "unfiltered",
        "map": "unfiltered",
        "entities": "partial",
    }


@pytest.mark.parametrize(
    ("editor", "names"),
    [
        pytest.param(
            'class E{constructor(){this._schema=(0,m.A)(e=>[{name:"entity"}])}}',
            ["entity"],
            id="memoized function",
        ),
        pytest.param(
            'var C=[{name:"title"},{name:"b,c"},{name:`d,${1}`}],x=1;'
            "class E{render(){return `<ha-form .schema=${C}>`}}",
            ["title", "b,c", "d,1"],
            id="constant with commas inside its strings",
        ),
    ],
)
def test_an_editor_form_is_found_as_a_function_or_a_constant(
    tmp_path: Path, editor: str, names: list[str]
) -> None:
    definitions = build_definitions(tmp_path, {"tile": editor})

    described = definitions.describe("tile")

    assert described is not None
    assert [field["name"] for field in described["fields"]] == names


# --- the index on a real hass ------------------------------------------------


async def test_card_warnings_never_fail_a_write(
    hass: HomeAssistant, frontend: FrontendOnDisk
) -> None:
    """The check is advice: a dashboard it cannot check still saves."""
    unreadable = {"views": [{"cards": [{"type": "tile", "entity": object()}]}]}
    flagged = {"views": [{"cards": [{"type": "tile", "colour": "red"}]}]}

    assert await cd.async_card_warnings(hass, unreadable) == []
    assert len(await cd.async_card_warnings(hass, flagged)) == 1


async def test_first_use_shares_one_background_index_build(
    hass: HomeAssistant, frontend: FrontendOnDisk
) -> None:
    frontend.hold()
    assert await cd.async_get_definitions(hass, timeout=0.01) is None
    assert await cd.async_get_definitions(hass, timeout=0.01) is None

    frontend.release()
    definitions = await cd.async_get_definitions(hass, timeout=10)

    assert definitions is not None
    assert await cd.async_get_definitions(hass) is definitions
    assert frontend.builds == 1
    frontend.runtime.assert_awaited_once()


async def test_a_stuck_index_build_cannot_hold_a_saved_write(
    hass: HomeAssistant, frontend: FrontendOnDisk, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"views": [{"cards": [{"type": "tyle"}]}]}
    frontend.hold()
    with monkeypatch.context() as short_budget:
        short_budget.setattr(cd, "_CARD_WORK_SECONDS", 0.05)

        warnings = await asyncio.wait_for(cd.async_card_warnings(hass, config), 2)

    assert warnings == []
    frontend.release()
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    assert await cd.async_card_warnings(hass, config) == [
        "views[0].cards[0]: unknown card type 'tyle'"
    ]


# --- server: ha_config_get_dashboard(describe=True) ---------------------------


async def test_describe_compacts_the_editor_form(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    frontend.write({"tile": _FORM_EDITOR})

    result = await describe_mod.describe_card_response(component_core_client, "tile")

    assert result["name"] == "Tile"
    assert result["description"] == "An entity at a glance."
    assert result["fields"] == [
        {"name": "entity", "type": "entity"},
        {
            "name": "color",
            "type": "ui_color",
            "description": "Inactive state is not colored.",
        },
    ]


async def test_describe_unknown_card_type_lists_the_real_ones(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, "tyle")

    assert "VALIDATION_INVALID_PARAMETER" in str(err.value)
    assert "entities, tile" in str(err.value)
    assert "not a known card type" in str(err.value)


async def test_describe_while_the_index_builds_says_retry(
    hass: HomeAssistant,
    frontend: FrontendOnDisk,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first describe after a start outlasts the command wait: the caller
    is told the index is building, not that the command timed out."""
    frontend.hold()
    with monkeypatch.context() as short_budget:
        short_budget.setattr(cd, "_CARD_WORK_SECONDS", 0.05)
        with pytest.raises(ToolError) as err:
            await describe_mod.describe_card_response(component_core_client, "tile")
    assert "still indexing" in str(err.value)
    assert "SERVICE_CALL_FAILED" in str(err.value)

    frontend.release()
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    described = await describe_mod.describe_card_response(component_core_client, "tile")
    assert described["name"] == "Tile"


async def test_describe_with_an_unreadable_frontend_points_to_the_static_guide(
    frontend: FrontendOnDisk, component_core_client: HomeAssistantClient
) -> None:
    frontend.break_files()

    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, "tile")

    assert "card definitions are unavailable" in str(err.value)
    assert "references/dashboard-cards.md" in str(err.value)


async def test_describe_without_the_component_says_so(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(core_client, "tile")

    assert "COMPONENT_NOT_INSTALLED" in str(err.value)


async def test_describe_timeout_reaches_the_caller_as_a_timeout(
    hass: HomeAssistant,
    frontend: FrontendOnDisk,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the index loaded, a describe that outlasts its budget is a timeout
    to retry, not an unavailable index."""
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    monkeypatch.setattr(cd, "_CARD_WORK_SECONDS", 0.05)

    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, None)

    assert "TIMEOUT_OPERATION" in str(err.value)


async def test_a_slow_custom_refresh_still_lists_the_builtin_cards(
    frontend: FrontendOnDisk,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cd, "_CUSTOM_WAIT_SECONDS", 0.05)

    result = await describe_mod.describe_card_response(component_core_client, None)

    assert [card["type"] for card in result["card_types"]] == ["entities", "tile"]
    assert result["custom_status"]["state"] == "loading"


async def test_describe_drops_stale_capabilities_after_unknown_command(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downgraded component must not keep routing describe to a command Core
    no longer registers."""
    monkeypatch.delitem(hass.data["websocket_api"], cd.WS_DASHBOARD_CARDS)
    invalidated = MagicMock(wraps=describe_mod.invalidate_caps)
    monkeypatch.setattr(describe_mod, "invalidate_caps", invalidated)

    with pytest.raises(ToolError):
        await describe_mod.describe_card_response(component_core_client, "tile")

    invalidated.assert_called_once_with(component_core_client)


# --- custom cards ----------------------------------------------------------


def test_resource_urls_map_to_files_inside_www(tmp_path: Path) -> None:
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
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = worker.encode()
        info = tarfile.TarInfo("package/worker.js")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_linkedom_is_checked_and_made_a_scoped_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


@pytest.mark.usefixtures("idle_custom_cards")
def test_custom_card_messages_read_plainly(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """A card throws what its ``setConfig`` is told to; the warning names the
    option and leaves out the struct's framing."""
    definitions = build_definitions(tmp_path)
    never = "At path: colour -- Expected a value of type `never`, but received: `1`"
    custom_cards.refresh(
        [
            card_file(
                tmp_path / "my-card.js",
                "my-card",
                "if (c.fail) throw new Error(c.fail);",
            )
        ]
    )
    fails = [
        never,
        never.replace("colour", "card_mod"),
        "At path: size -- Expected a number",
        "value.series is missing",
    ]
    cards = [{"type": "custom:my-card", "fail": message} for message in fails]

    warnings = definitions.validate({"views": [{"cards": cards}]}, custom_cards)

    assert warnings == [
        "views[0].cards[0] (custom:my-card): 'colour' is not a my-card card option",
        "views[0].cards[2] (custom:my-card): size: Expected a number",
        "views[0].cards[3] (custom:my-card): value.series is missing",
    ]


@pytest.mark.usefixtures("idle_custom_cards")
def test_custom_cards_report_their_own_problems_and_unknown_ones_are_named(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    definitions = build_definitions(tmp_path)
    custom_cards.refresh(
        [
            card_file(tmp_path / "good.js", "good-card"),
            card_file(
                tmp_path / "picky.js",
                "picky-card",
                "throw new Error('picky-card needs an entity');",
            ),
        ]
    )
    cards = [
        {"type": "custom:good-card"},
        {"type": "custom:picky-card"},
        {"type": "custom:not-installed-card"},
    ]

    warnings = definitions.validate({"views": [{"cards": cards}]}, custom_cards)

    assert warnings == [
        "views[0].cards[1] (custom:picky-card): picky-card needs an entity",
        "views[0].cards[2] (custom:not-installed-card): not found in dashboard "
        + "resources; check the type spelling or whether it loads through extra "
        + "JavaScript.",
    ]


@pytest.mark.usefixtures("idle_custom_cards")
def test_an_unknown_custom_card_is_not_reported_when_a_resource_could_not_run(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """A resource that did not run may have defined the card."""
    definitions = build_definitions(tmp_path)
    broken = tmp_path / "broken.js"
    broken.write_text("invalid code", encoding="utf-8")
    custom_cards.refresh([broken])
    cards = [{"type": "custom:maybe-in-the-broken-resource"}]

    assert definitions.validate({"views": [{"cards": cards}]}, custom_cards) == []


def test_removed_resources_are_unloaded(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    card = card_file(tmp_path / "a-card.js", "a-card")
    custom_cards.refresh([card])
    assert custom_cards.check("a-card", {"type": "custom:a-card"}) == []

    custom_cards.refresh([])

    assert custom_cards.check("a-card", {"type": "custom:a-card"}) is None


def test_deleted_resource_files_are_evicted(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    card = card_file(tmp_path / "deleted.js", "a-card")
    custom_cards.refresh([card])
    assert custom_cards.check("a-card", {}) == []

    card.unlink()
    custom_cards.refresh([card])  # The resource registration still exists.

    assert custom_cards.check("a-card", {}) is None
    assert custom_cards.status()["state"] == "partial"


def test_capacity_skipped_resources_retry_without_file_changes(
    tmp_path: Path, custom_cards: cc.CustomCards, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "_BUNDLE_MEMORY", _PER_CARD)
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", _PER_CARD)  # room for one card
    files = [card_file(tmp_path / f"{tag}.js", tag) for tag in ("a-card", "b-card")]

    custom_cards.refresh(files)
    assert custom_cards.check("b-card", {}) is None
    assert custom_cards.status()["state"] == "partial"

    custom_cards.refresh(files[1:])  # the first one's room is free again

    assert custom_cards.check("b-card", {}) == []


def test_bundle_admission_reserves_room_for_later_heap_growth(
    tmp_path: Path, custom_cards: cc.CustomCards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each card is admitted at its full heap allowance, not at what it uses
    now, so two cards using little still leave no room for a third."""
    monkeypatch.setattr(cc, "_BUNDLE_MEMORY", _PER_CARD)
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", _PER_CARD * 5 // 2)
    tags = ("a-card", "b-card", "c-card")

    custom_cards.refresh([card_file(tmp_path / f"{tag}.js", tag) for tag in tags])

    assert [custom_cards.check(tag, {}) for tag in tags] == [[], [], None]


def test_failed_resources_stay_cached_but_a_type_change_reloads(
    tmp_path: Path,
    custom_cards: cc.CustomCards,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A card that only runs as an ES module fails as a script: that failure is
    not retried on every request, but re-registering it as a module loads it."""
    path = tmp_path / "module-card.js"
    path.write_text(
        "void import.meta;\n"
        "class Card extends HTMLElement { setConfig(c) {} }\n"
        "customElements.define('a-card', Card);\n",
        encoding="utf-8",
    )

    def failed_loads() -> int:
        return sum("did not load" in record.getMessage() for record in caplog.records)

    with caplog.at_level(logging.DEBUG, logger=cc.__name__):
        custom_cards.refresh([path])
        custom_cards.refresh([path])
        assert failed_loads() == 1
        assert custom_cards.check("a-card", {}) is None

        custom_cards.refresh([path], {path})

    assert custom_cards.check("a-card", {}) == []


def test_oversized_card_files_are_not_run(
    tmp_path: Path, custom_cards: cc.CustomCards, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = card_file(tmp_path / "small-card.js", "small-card")
    big = card_file(tmp_path / "huge-card.js", "huge-card")
    big.write_text(big.read_text(encoding="utf-8") + "// padding\n", encoding="utf-8")
    monkeypatch.setattr(cc, "_MAX_SOURCE_BYTES", small.stat().st_size)

    custom_cards.refresh([small, big])

    assert custom_cards.check("small-card", {}) == []
    assert custom_cards.check("huge-card", {}) is None
    assert custom_cards.status()["state"] == "partial"


def test_engine_calls_skip_quickjs_full_garbage_collection(
    tmp_path: Path, custom_cards: cc.CustomCards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """quickjs-ng's ``run_gc`` default runs ``gc.collect()`` under the GIL on
    every call, which stalls Home Assistant's event loop (#2744)."""
    import quickjs

    with counted_full_collections(monkeypatch) as collections:
        definitions = build_definitions(tmp_path)
        definitions.describe("tile")
        definitions.validate({"views": [{"cards": [{"type": "tile", "x": 1}]}]})
        custom_cards.refresh([card_file(tmp_path / "a-card.js", "a-card")])
        custom_cards.check("a-card", {})
        custom_cards.describe("a-card")
        assert collections == []

        quickjs.Function("f", "function f() { return 1; }")()
        assert collections == ["gc"]
