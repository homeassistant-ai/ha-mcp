"""Card definitions read from Home Assistant's frontend (#2632).

The component builds an index from the frontend Core serves (the installed
``home-assistant-frontend``), checks a saved dashboard against it, and answers
``dashboard_cards`` with a card's editor form. These run the index on that real
frontend, the command and the describe tool on a real ``hass``, and custom
cards through their real QuickJS sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._card_definitions_core import (
    CardIndex,
    assert_described_without_a_form,
    card_description,
    card_file,
    card_name,
    cc,
    cd,
    counted_full_collections,
    dcw,
    describe_mod,
    real_definitions,
)
from ._card_definitions_core import card_index as card_index
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import definitions as definitions
from ._card_definitions_core import fresh_card_state as fresh_card_state
from ._card_definitions_core import stalled_card_download as stalled_card_download

__all__ = ["cc", "cd", "dcw", "describe_mod"]

_PER_CARD = 16 * 1024 * 1024  # one sandbox's heap allowance in these tests
_ADVISORY = "editor schema advisory; runtime support may differ"
# A card type no frontend registers.
_NO_SUCH_CARD = "no-such-card"
# Built-in cards whose editor has no form.
_CARDS_WITHOUT_A_FORM = (
    "vertical-stack",
    "horizontal-stack",
    "entity-filter",
    "shopping-list",
)


def _paths(warnings: list[str]) -> list[str]:
    return [warning.split(":")[0] for warning in warnings]


def _views(*cards: dict[str, Any]) -> dict[str, Any]:
    return {"views": [{"cards": list(cards)}]}


def _option_names(fields: list[dict[str, Any]]) -> list[str]:
    """The config keys a described form stores: an unnamed or flattened group's
    fields sit beside its siblings, a named group nests under its own key."""
    names: list[str] = []
    for field in fields:
        children = field.get("schema")
        if isinstance(children, list) and (
            not field.get("name") or field.get("flatten")
        ):
            names.extend(_option_names(children))
        elif field.get("name"):
            names.append(field["name"])
    return names


# --- the index on the real frontend -------------------------------------------


def test_every_card_position_is_checked_except_an_entity_filters_card(
    definitions: cd.CardDefinitions,
) -> None:
    """A card in a stack, a section or a conditional is checked; an
    entity-filter's ``card`` is not (its parent fills it in)."""
    config = {
        "views": [
            {
                "cards": [
                    {"type": _NO_SUCH_CARD},
                    {"type": "vertical-stack", "cards": [{"type": _NO_SUCH_CARD}]},
                    {
                        "type": "conditional",
                        "conditions": [],
                        "card": {"type": _NO_SUCH_CARD},
                    },
                    {
                        "type": "entity-filter",
                        "entities": [],
                        "card": {"title": "rows"},
                    },
                ],
                "sections": [{"cards": [{"type": _NO_SUCH_CARD}]}, "not a section"],
            },
            {"strategy": {"type": "original-states"}},
        ]
    }

    assert _paths(definitions.validate(config)) == [
        "views[0].cards[0]",
        "views[0].cards[1].cards[0]",
        "views[0].cards[2].card",
        "views[0].sections[0].cards[0]",
    ]


def test_a_card_without_a_type_or_with_a_misspelt_one_is_flagged(
    definitions: cd.CardDefinitions,
) -> None:
    cards: list[dict[str, Any]] = [
        {"entity": "light.x"},
        {"type": "tyle"},
        {"type": "custom:x-card"},
        {"type": "tile", "entity": "light.x"},
    ]

    warnings = definitions.validate(_views(*cards))

    assert warnings == [
        "views[0].cards[0]: no card type configured",
        "views[0].cards[1]: unknown card type 'tyle'",
    ]


def test_header_and_a_custom_cards_children_are_checked_but_not_its_metadata(
    definitions: cd.CardDefinitions,
) -> None:
    config = {
        "views": [
            {
                "header": {"card": {"type": _NO_SUCH_CARD}},
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
        f"views[0].header.card: unknown card type '{_NO_SUCH_CARD}'",
        'views[0].cards[0].custom_fields["nested"].card.states["on"]: '
        + "unknown card type 'tyle'",
    ]


def test_warnings_are_capped_and_say_how_many_were_left_out(
    definitions: cd.CardDefinitions,
) -> None:
    few = definitions.validate(_views({"type": "a"}, {"type": "b"}))
    many = definitions.validate(_views(*({"type": f"t{i}"} for i in range(25))))

    assert len(few) == 2
    left_out = 25 - (len(many) - 1)
    assert 0 < left_out < 25
    assert many[-1] == f"...and {left_out} more card diagnostics"


def test_an_option_the_editor_does_not_list_is_flagged_but_card_mod_is_not(
    definitions: cd.CardDefinitions,
) -> None:
    cards: list[dict[str, Any]] = [
        {"type": "tile", "colour": "red"},
        {"type": "tile", "card_mod": {"style": "x"}},
        {"type": "tile", "tap_action": {"action": "bad"}},
        {"type": "tile", "entity": "light.x"},
    ]

    warnings = definitions.validate(_views(*cards))

    assert len(warnings) == 2
    assert warnings[0] == (
        f"views[0].cards[0] (tile): {_ADVISORY}: "
        "'colour' is not listed in the tile editor schema"
    )
    assert warnings[1].startswith(
        f"views[0].cards[2] (tile): {_ADVISORY}: tap_action.action: Expected one of"
    )
    assert '"bad"' in warnings[1]


def test_card_types_carry_the_names_the_frontend_translates(
    definitions: cd.CardDefinitions,
) -> None:
    listed = {card["type"]: card for card in definitions.card_types()}

    for card_type in ("tile", "entities"):
        assert listed[card_type]["name"] == card_name(card_type)
        assert listed[card_type]["description"] == card_description(card_type)
    # A card the frontend registers but never names is known, not listed.
    assert "shopping-list" not in listed
    assert definitions.validate(_views({"type": "shopping-list"})) == []


def test_the_index_keeps_inherited_cards_but_excludes_dialogs(
    definitions: cd.CardDefinitions,
) -> None:
    """A card with no translated name is a card when it implements, or inherits,
    ``setConfig`` (``shopping-list``); a dialog registered under a ``hui-*-card``
    tag is not one."""
    cards: list[dict[str, Any]] = [
        {"type": "horizontal-stack", "cards": []},
        {"type": "dialog-edit"},
        {"type": "shopping-list"},
    ]

    assert definitions.validate(_views(*cards)) == [
        "views[0].cards[1]: unknown card type 'dialog-edit'"
    ]


@pytest.mark.parametrize("card_type", _CARDS_WITHOUT_A_FORM)
def test_a_card_without_an_editor_form_lists_no_fields_and_claims_no_coverage(
    definitions: cd.CardDefinitions, card_type: str
) -> None:
    assert_described_without_a_form(definitions.describe(card_type))


def test_every_option_a_form_lists_is_accepted_and_an_unlisted_one_is_flagged(
    definitions: cd.CardDefinitions,
) -> None:
    """A description that listed an editor-only option would have the caller write
    a config the card rejects; the validator and the description agree for every
    built-in card whose stored struct could be read."""
    checked = []
    for card in definitions.card_types():
        card_type = card["type"]
        described = definitions.describe(card_type)
        assert described is not None
        if described.get("field_coverage") != "partial":
            continue
        options = dict.fromkeys(_option_names(described["fields"]))
        listed = definitions.validate(_views({"type": card_type, **options}))
        unlisted = definitions.validate(_views({"type": card_type, "unlisted": 1}))
        assert not [w for w in listed if "is not listed" in w], card_type
        assert [w for w in unlisted if "'unlisted' is not listed" in w], card_type
        checked.append(card_type)

    assert {"tile", "entity", "area", "map"} <= set(checked)


def test_describe_lists_the_stored_options_the_form_lacks_once(
    definitions: cd.CardDefinitions,
) -> None:
    described = definitions.describe("tile")

    assert described is not None
    names = [field["name"] for field in described["fields"]]
    assert len(names) == len(set(names))
    assert {"vertical", "features"} <= set(names)
    vertical = next(f for f in described["fields"] if f["name"] == "vertical")
    assert vertical["type"] == "boolean"


@pytest.mark.parametrize("card_type", ["entities", "glance"])
def test_a_form_kept_in_a_module_constant_is_found(
    definitions: cd.CardDefinitions, card_type: str
) -> None:
    """These cards' editors hand their form to ``ha-form`` as a constant, not
    from a memoized function."""
    described = definitions.describe(card_type)

    assert described is not None
    assert "title" in [field["name"] for field in described["fields"]]


def test_a_threshold_group_the_form_nests_keeps_its_fields(
    definitions: cd.CardDefinitions,
) -> None:
    described = definitions.describe("gauge")

    assert described is not None
    severity = next(f for f in described["fields"] if f["name"] == "severity")
    assert [f["name"] for f in severity["schema"]] == ["green", "yellow", "red"]


def test_a_form_that_needs_selected_options_still_describes(
    definitions: cd.CardDefinitions,
) -> None:
    """The alarm panel's form reads which states are available; with none
    selected it still lists its states."""
    described = definitions.describe("alarm-panel")

    assert described is not None
    states = next(f for f in described["fields"] if f["name"] == "states")
    assert states["selector"]["select"]["options"]


# --- the index on a real hass ------------------------------------------------


async def test_card_warnings_never_fail_a_write(
    hass: HomeAssistant, card_index: CardIndex
) -> None:
    """The check is advice: a dashboard it cannot check still saves."""
    unreadable = {"views": [{"cards": [{"type": "tile", "entity": object()}]}]}
    flagged = {"views": [{"cards": [{"type": "tile", "colour": "red"}]}]}

    assert await cd.async_card_warnings(hass, unreadable) == []
    assert len(await cd.async_card_warnings(hass, flagged)) == 1


async def test_first_use_shares_one_background_index_build(
    hass: HomeAssistant, card_index: CardIndex
) -> None:
    card_index.frontend.hold()
    assert await cd.async_get_definitions(hass, timeout=0.01) is None
    assert await cd.async_get_definitions(hass, timeout=0.01) is None

    card_index.frontend.release()
    definitions = await cd.async_get_definitions(hass, timeout=10)

    assert definitions is not None
    assert await cd.async_get_definitions(hass) is definitions
    assert card_index.frontend.calls == 1
    card_index.runtime.assert_awaited_once()


async def test_a_stuck_index_build_cannot_hold_a_saved_write(
    hass: HomeAssistant, card_index: CardIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _views({"type": "tyle"})
    card_index.frontend.hold()
    with monkeypatch.context() as short_budget:
        short_budget.setattr(cd, "CARD_WORK_SECONDS", 0.05)

        warnings = await asyncio.wait_for(cd.async_card_warnings(hass, config), 2)

    assert warnings == []
    card_index.frontend.release()
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    assert await cd.async_card_warnings(hass, config) == [
        "views[0].cards[0]: unknown card type 'tyle'"
    ]


# --- server: ha_config_get_dashboard(describe=True) ---------------------------


async def test_describe_compacts_the_editor_form(
    card_index: CardIndex, component_core_client: HomeAssistantClient
) -> None:
    result = await describe_mod.describe_card_response(component_core_client, "tile")

    assert result["name"] == card_name("tile")
    assert result["description"] == card_description("tile")
    fields = {field["name"]: field for field in result["fields"]}
    assert fields["entity"] == {"name": "entity", "type": "entity"}
    assert fields["color"]["type"] == "ui_color"
    assert fields["color"]["description"]
    # The form's flattened and unnamed groups are inlined, not listed.
    assert not {"content", "interactions"} & set(fields)


async def test_describe_unknown_card_type_lists_the_real_ones(
    card_index: CardIndex, component_core_client: HomeAssistantClient
) -> None:
    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, "tyle")

    assert "VALIDATION_INVALID_PARAMETER" in str(err.value)
    assert "tile" in str(err.value)
    assert "not a known card type" in str(err.value)


async def test_describe_while_the_index_builds_says_retry(
    hass: HomeAssistant,
    card_index: CardIndex,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first describe after a start outlasts the command wait: the caller
    is told the index is building, not that the command timed out."""
    card_index.frontend.hold()
    with monkeypatch.context() as short_budget:
        short_budget.setattr(cd, "CARD_WORK_SECONDS", 0.05)
        with pytest.raises(ToolError) as err:
            await describe_mod.describe_card_response(component_core_client, "tile")
    assert "still indexing" in str(err.value)
    assert "SERVICE_CALL_FAILED" in str(err.value)

    card_index.frontend.release()
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    described = await describe_mod.describe_card_response(component_core_client, "tile")
    assert described["name"] == card_name("tile")


async def test_describe_with_an_unreadable_frontend_points_to_the_static_guide(
    card_index: CardIndex, component_core_client: HomeAssistantClient
) -> None:
    card_index.frontend.unreadable()

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
    card_index: CardIndex,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the index loaded, a describe that outlasts its budget is a timeout
    to retry, not an unavailable index."""
    assert await cd.async_get_definitions(hass, timeout=10) is not None
    monkeypatch.setattr(cd, "CARD_WORK_SECONDS", 0.05)

    with pytest.raises(ToolError) as err:
        await describe_mod.describe_card_response(component_core_client, None)

    assert "TIMEOUT_OPERATION" in str(err.value)


async def test_a_slow_custom_refresh_still_lists_the_builtin_cards(
    card_index: CardIndex,
    stalled_card_download: None,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cd, "CUSTOM_WAIT_SECONDS", 0.05)

    result = await describe_mod.describe_card_response(component_core_client, None)

    assert {"tile", "entities"} <= {card["type"] for card in result["card_types"]}
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
    assert cc.resource_path(tmp_path, "/local/community/mushroom/mushroom.js?v=1") == (
        (www / "community" / "mushroom" / "mushroom.js").resolve()
    )
    assert cc.resource_path(tmp_path, "/local/cards/my%20card.js") == (
        (www / "cards" / "my card.js").resolve()
    )
    for url in (
        "/local/../secrets.yaml",
        "/local/community/../../configuration.js",
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


@pytest.mark.usefixtures("fresh_card_state")
def test_custom_card_messages_read_plainly(
    tmp_path: Path, custom_cards: cc.CustomCards, definitions: cd.CardDefinitions
) -> None:
    """A card throws what its ``setConfig`` is told to; the warning names the
    option and leaves out the struct's framing."""
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

    warnings = definitions.validate(_views(*cards), custom_cards)

    assert warnings == [
        "views[0].cards[0] (custom:my-card): 'colour' is not a my-card card option",
        "views[0].cards[2] (custom:my-card): size: Expected a number",
        "views[0].cards[3] (custom:my-card): value.series is missing",
    ]


@pytest.mark.usefixtures("fresh_card_state")
def test_custom_cards_report_their_own_problems_and_unknown_ones_are_named(
    tmp_path: Path, custom_cards: cc.CustomCards, definitions: cd.CardDefinitions
) -> None:
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

    warnings = definitions.validate(_views(*cards), custom_cards)

    assert warnings == [
        "views[0].cards[1] (custom:picky-card): picky-card needs an entity",
        "views[0].cards[2] (custom:not-installed-card): not found in dashboard "
        + "resources; check the type spelling or whether it loads through extra "
        + "JavaScript.",
    ]


@pytest.mark.usefixtures("fresh_card_state")
def test_an_unknown_custom_card_is_not_reported_when_a_resource_could_not_run(
    tmp_path: Path, custom_cards: cc.CustomCards, definitions: cd.CardDefinitions
) -> None:
    """A resource that did not run may have defined the card."""
    broken = tmp_path / "broken.js"
    broken.write_text("invalid code", encoding="utf-8")
    custom_cards.refresh([broken])
    cards = [{"type": "custom:maybe-in-the-broken-resource"}]

    assert definitions.validate(_views(*cards), custom_cards) == []


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
    monkeypatch.setattr(cc, "BUNDLE_MEMORY", _PER_CARD)
    monkeypatch.setattr(cc, "TOTAL_MEMORY", _PER_CARD)  # room for one card
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
    monkeypatch.setattr(cc, "BUNDLE_MEMORY", _PER_CARD)
    monkeypatch.setattr(cc, "TOTAL_MEMORY", _PER_CARD * 5 // 2)
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
    monkeypatch.setattr(cc, "MAX_SOURCE_BYTES", small.stat().st_size)

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
        definitions = real_definitions()
        definitions.describe("tile")
        definitions.validate(_views({"type": "tile", "x": 1}))
        custom_cards.refresh([card_file(tmp_path / "a-card.js", "a-card")])
        custom_cards.check("a-card", {})
        custom_cards.describe("a-card")
        assert collections == []

        quickjs.Function("f", "function f() { return 1; }")()
        assert collections == ["gc"]
