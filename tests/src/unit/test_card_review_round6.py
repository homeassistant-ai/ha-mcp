"""Regressions reported by Patch76 and reproduced on embedded HA 2026.10.

The card index runs on a small frontend build written to disk, the index
build on a real ``hass``, and custom cards through their real QuickJS sandbox.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ._card_definitions_core import (
    FrontendOnDisk,
    build_definitions,
    cc,
    cd,
)
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import frontend as frontend
from ._card_definitions_core import idle_custom_cards as idle_custom_cards


def _struct_editor(struct: str, form: str) -> str:
    """An editor module asserting its config against ``struct`` and showing ``form``."""
    return (
        "var s=r(6),v="
        + struct
        + ";class C{constructor(){this._schema=(0,s.memo)(e=>"
        + form
        + ")}setConfig(c){(0,s.assert)(c,v)}}"
    )


def _card_source(tag: str, *members: str) -> str:
    """A custom card element ``tag`` whose class body is ``members``."""
    name = tag.replace("-", "_")
    body = "\n".join(members)
    return (
        f"class {name} extends HTMLElement {{\n{body}\n}}\n"
        f"customElements.define({tag!r}, {name});\n"
    )


def _write_card(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def _custom_warnings(
    tmp_path: Path, custom_cards: cc.CustomCards, tags: list[str]
) -> list[str]:
    """What a dashboard save says about one custom card per tag."""
    definitions = build_definitions(tmp_path)
    cards = [{"type": f"custom:{tag}", "entities": 5} for tag in tags]
    return definitions.validate({"views": [{"cards": cards}]}, custom_cards)


@pytest.mark.parametrize(
    ("severity", "kept"),
    [
        pytest.param('{type:"object"}', ["green", "yellow", "red"], id="open object"),
        pytest.param(
            "(0,s.object)({green:s.string})", ["green"], id="object with listed keys"
        ),
    ],
)
def test_open_object_struct_keeps_editor_threshold_fields(
    tmp_path: Path, severity: str, kept: list[str]
) -> None:
    """A struct that takes any object under ``severity`` does not drop the
    threshold fields the editor shows; one that lists keys still does."""
    struct = (
        "{schema:{type:s.string,severity:" + severity + "},validate:()=>[void 0,{}]}"
    )
    form = '[{name:"severity",schema:[{name:"green"},{name:"yellow"},{name:"red"}]}]'
    editor = _struct_editor(struct, form)
    definitions = build_definitions(tmp_path, {"tile": editor})

    described = definitions.describe("tile")

    assert described is not None
    assert described["fields"] == [
        {"name": "severity", "schema": [{"name": name} for name in kept]}
    ]


def test_alarm_form_can_describe_options_without_selected_states(
    tmp_path: Path,
) -> None:
    editor = (
        "class E{constructor(){this._schema=(0,m.A)((localize,stateObj,states)=>"
        '[{name:"states",selector:{select:{options:["armed_home"].map('
        "o=>({value:o,disabled:!states.includes(o)}))}}}])}}"
    )
    definitions = build_definitions(tmp_path, {"tile": editor})

    described = definitions.describe("tile")

    assert described is not None
    assert described["fields"][0]["selector"]["select"]["options"] == [
        {"value": "armed_home", "disabled": True}
    ]


def test_form_defaults_are_not_overridden_by_empty_selected_options(
    tmp_path: Path,
) -> None:
    editor = (
        "class E{constructor(){this._schema=(0,m.A)("
        "(localize,entity,icon='mdi:link')=>"
        "[{name:'icon',selector:{icon:{placeholder:icon}}}])}}"
    )
    definitions = build_definitions(tmp_path, {"tile": editor})

    described = definitions.describe("tile")

    assert described is not None
    assert described["fields"][0]["selector"]["icon"]["placeholder"] == "mdi:link"


@pytest.mark.parametrize("failure", ["runtime", "index"])
async def test_failed_initialization_retries_after_cooldown(
    hass: HomeAssistant,
    frontend: FrontendOnDisk,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A card index that failed to start is not rebuilt on every request; once
    the cooldown ends, concurrent callers share one new build."""
    if failure == "runtime":
        frontend.runtime.side_effect = [False, True]
    else:
        (frontend.root / "frontend_latest" / "cards.js").unlink()

    assert await cd.async_get_definitions(hass) is None
    assert await cd.async_get_definitions(hass) is None
    frontend.runtime.assert_awaited_once()

    monkeypatch.setattr(cd, "_BUILD_RETRY_SECONDS", 0)
    if failure == "index":
        frontend.write()
    first, second = await asyncio.gather(
        cd.async_get_definitions(hass), cd.async_get_definitions(hass)
    )

    assert first is not None
    assert second is first
    assert frontend.runtime.await_count == 2
    assert await cd.async_get_definitions(hass) is first
    assert frontend.runtime.await_count == 2


def test_missing_builtin_form_has_no_field_coverage_claim(tmp_path: Path) -> None:
    """A card with no editor lists no fields, and says nothing about coverage."""
    definitions = build_definitions(tmp_path, cards=("alert",))

    described = definitions.describe("alert")

    assert described is not None
    assert described["fields"] is None
    assert "field_coverage" not in described
    assert "note" not in described


_BROKEN_EDITORS = {
    "throws": "static getConfigElement() { throw new Error('editor unavailable'); }",
    "never_returns": "static getConfigElement() { while (true) {} }",
}


@pytest.fixture(params=sorted(_BROKEN_EDITORS))
def broken_editor_card(
    request: pytest.FixtureRequest, tmp_path: Path, custom_cards: cc.CustomCards
) -> str:
    """The tag of a loaded custom card whose editor cannot be prepared."""
    path = _write_card(
        tmp_path / "test-card.js",
        _card_source(
            "test-card",
            "setConfig(c) {if (!c.entity) throw new Error('needs an entity');}",
            _BROKEN_EDITORS[request.param],
        ),
    )
    custom_cards.refresh([path])
    return "test-card"


def test_editor_failure_does_not_hide_card_verdict(
    custom_cards: cc.CustomCards, broken_editor_card: str
) -> None:
    expected = [{"source": "card", "message": "needs an entity"}]

    assert custom_cards.check(broken_editor_card, {}) == expected
    assert custom_cards.check(broken_editor_card, {}) == expected
    assert custom_cards.check(broken_editor_card, {"entity": "light.test"}) == []


def test_missing_custom_form_has_no_field_coverage_claim(
    custom_cards: cc.CustomCards, broken_editor_card: str
) -> None:
    described = custom_cards.describe(broken_editor_card)

    assert described is not None
    assert described["fields"] is None
    assert "field_coverage" not in described
    assert "note" not in described


def test_looping_editor_cannot_consume_later_cards_budget(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """A looping editor is cut off at its own short budget, so a card checked
    afterwards that needs longer than that still gets its full time."""
    source = _card_source(
        "looping-editor-card",
        "setConfig() {throw new Error('card verdict');}",
        "static getConfigElement() {return {setConfig() {while (true) {}}};}",
    ) + _card_source(
        "slow-card",
        "setConfig() {const t = Date.now(); while (Date.now() - t < 1000) {}"
        " throw new Error('slow verdict');}",
    )
    custom_cards.refresh([_write_card(tmp_path / "cards.js", source)])
    looping = [{"source": "card", "message": "card verdict"}]

    assert custom_cards.check("looping-editor-card", {}) == looping
    assert custom_cards.check("slow-card", {}) == [
        {"source": "card", "message": "slow verdict"}
    ]
    assert custom_cards.check("looping-editor-card", {}) == looping


@pytest.mark.usefixtures("idle_custom_cards")
def test_looping_editors_do_not_exhaust_the_custom_save_budget(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """One editor that never returns cannot use up the time a save gives all
    its custom cards: every card is still checked."""
    tags = ["a-loop-card", "b-loop-card", "c-loop-card"]
    source = "".join(
        _card_source(
            tag,
            "setConfig() {throw new Error('card verdict');}",
            "static getConfigElement() {while (true) {}}",
        )
        for tag in tags
    )
    custom_cards.refresh([_write_card(tmp_path / "cards.js", source)])

    warnings = _custom_warnings(tmp_path, custom_cards, tags)

    assert warnings == [
        f"views[0].cards[{i}] (custom:{tag}): card verdict"
        for i, tag in enumerate(tags)
    ]


def test_synchronous_editor_form_does_not_drain_unrelated_timers(
    tmp_path: Path, custom_cards: cc.CustomCards
) -> None:
    """The editor of a card whose form is ready at once is not waited on, so a
    timer some other code scheduled (here one that never finishes) does not
    make the form unavailable."""
    source = _card_source(
        "sync-editor-card",
        "static getConfigForm() {return {schema: [{name: 'entity', selector: {entity: {}}}]};}",
        "static getConfigElement() {setTimeout(() => {while (true) {}});"
        " return {setConfig() {}};}",
    )
    custom_cards.refresh([_write_card(tmp_path / "card.js", source)])

    described = custom_cards.describe("sync-editor-card")

    assert described is not None
    assert described["fields"] == [{"name": "entity", "selector": {"entity": {}}}]


@pytest.mark.usefixtures("idle_custom_cards")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("throw new TypeError('entity must be a string')", id="thrown"),
        pytest.param("c.entities.map(x => x)", id="not a function"),
        pytest.param("this.attachInternals()", id="browser api"),
    ],
)
def test_type_errors_are_inconclusive_instead_of_silently_discarded(
    tmp_path: Path, custom_cards: cc.CustomCards, body: str
) -> None:
    source = _card_source("type-error-card", f"setConfig(c) {{{body}}}")
    custom_cards.refresh([_write_card(tmp_path / "card.js", source)])

    [warning] = _custom_warnings(tmp_path, custom_cards, ["type-error-card"])

    assert warning.startswith("views[0].cards[0] (custom:type-error-card): ")
    assert "configuration check inconclusive (TypeError: " in warning
    assert "does not establish invalid configuration" in warning
    if body.startswith("throw"):
        assert "entity must be a string" in warning


@pytest.mark.parametrize(
    ("grown", "reason"),
    [
        pytest.param(b";" * 65, "source size limit", id="one byte over"),
        pytest.param(
            ("é" * 33).encode(), "source size limit", id="over in bytes, not characters"
        ),
        pytest.param(b";" * 64, None, id="exactly at the limit"),
    ],
)
def test_source_growth_after_stat_is_bounded_before_runtime_creation(
    tmp_path: Path,
    custom_cards: cc.CustomCards,
    monkeypatch: pytest.MonkeyPatch,
    grown: bytes,
    reason: str | None,
) -> None:
    """A file that grows between being measured and being read is still held
    to the size limit; one that stays within it loads."""
    path = tmp_path / "growing-card.js"
    path.write_bytes(b";")
    monkeypatch.setattr(cc, "_MAX_SOURCE_BYTES", 64)
    measured = Path.stat

    def stat_then_grow(self: Path, **kwargs: Any) -> os.stat_result:
        result = measured(self, **kwargs)
        if self == path:
            path.write_bytes(grown)
        return result

    with monkeypatch.context() as racing:
        racing.setattr(Path, "stat", stat_then_grow)
        custom_cards.refresh([path])

    skipped = [{"resource": str(path), "reason": reason}] if reason else []
    assert custom_cards.status() == {
        "state": "partial" if reason else "ready",
        "resources": skipped,
    }


@pytest.mark.parametrize(
    ("card_type", "key", "other"),
    [
        ("area", "show_name", "show_camera"),
        ("todo-list", "tap_action", "item_tap_action"),
    ],
)
def test_advice_does_not_rename_options_with_different_semantics(
    tmp_path: Path, card_type: str, key: str, other: str
) -> None:
    """An option the editor does not list is reported as not listed, even when
    the editor lists a similarly named option that means something else."""
    struct = f"(0,s.object)({{type:s.string,{other}:s.string}})"
    editor = _struct_editor(struct, f'[{{name:"{other}"}}]')
    definitions = build_definitions(tmp_path, {card_type: editor}, cards=(card_type,))
    cards = [{"type": card_type, key: "x"}, {"type": card_type, other: "x"}]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert warnings == [
        f"views[0].cards[0] ({card_type}): editor schema advisory; runtime "
        f"support may differ: '{key}' is not listed in the {card_type} editor schema"
    ]
