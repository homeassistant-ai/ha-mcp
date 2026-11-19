"""Regressions reported by Patch76 and reproduced on embedded HA 2026.10.

The card index runs on the installed frontend, the index build on a real
``hass``, and custom cards through their real QuickJS sandbox.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ._card_definitions_core import (
    CardIndex,
    assert_described_without_a_form,
    cc,
    cd,
    constrained_core,
)
from ._card_definitions_core import card_index as card_index
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import definitions as definitions
from ._card_definitions_core import fresh_card_state as fresh_card_state


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
    definitions: cd.CardDefinitions, custom_cards: cc.CustomCards, tags: list[str]
) -> list[str]:
    """What a dashboard save says about one custom card per tag."""
    cards = [{"type": f"custom:{tag}", "entities": 5} for tag in tags]
    return definitions.validate({"views": [{"cards": cards}]}, custom_cards)


@pytest.mark.parametrize("failure", ["runtime", "index"])
async def test_failed_initialization_retries_after_cooldown(
    hass: HomeAssistant,
    card_index: CardIndex,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A card index that failed to start is not rebuilt on every request; once
    the cooldown ends, concurrent callers share one new build."""
    with ExitStack() as failing:
        if failure == "runtime":
            # Home Assistant pins another QuickJS provider: the real check refuses.
            failing.enter_context(
                constrained_core(monkeypatch, tmp_path, "quickjs-ng==0.0.0\n")
            )
        else:
            card_index.frontend.without_cards()

        assert await cd.async_get_definitions(hass) is None
        assert await cd.async_get_definitions(hass) is None
        card_index.runtime.assert_awaited_once()

    card_index.frontend.readable()
    monkeypatch.setattr(cd, "BUILD_RETRY_SECONDS", 0)
    first, second = await asyncio.gather(
        cd.async_get_definitions(hass), cd.async_get_definitions(hass)
    )

    assert first is not None
    assert second is first
    assert card_index.runtime.await_count == 2
    assert await cd.async_get_definitions(hass) is first
    assert card_index.runtime.await_count == 2


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
    assert_described_without_a_form(custom_cards.describe(broken_editor_card))


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


@pytest.mark.usefixtures("fresh_card_state")
def test_looping_editors_do_not_exhaust_the_custom_save_budget(
    tmp_path: Path, custom_cards: cc.CustomCards, definitions: cd.CardDefinitions
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

    warnings = _custom_warnings(definitions, custom_cards, tags)

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


@pytest.mark.usefixtures("fresh_card_state")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("throw new TypeError('entity must be a string')", id="thrown"),
        pytest.param("c.entities.map(x => x)", id="not a function"),
        pytest.param("this.attachInternals()", id="browser api"),
    ],
)
def test_type_errors_are_inconclusive_instead_of_silently_discarded(
    tmp_path: Path,
    custom_cards: cc.CustomCards,
    definitions: cd.CardDefinitions,
    body: str,
) -> None:
    source = _card_source("type-error-card", f"setConfig(c) {{{body}}}")
    custom_cards.refresh([_write_card(tmp_path / "card.js", source)])

    [warning] = _custom_warnings(definitions, custom_cards, ["type-error-card"])

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
    monkeypatch.setattr(cc, "MAX_SOURCE_BYTES", 64)
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
        # Options the installed frontend's editor does not list, beside a similar
        # one it does: the frontend's ``show_camera`` and ``item_tap_action``.
        ("area", "show_name", "show_camera"),
        ("todo-list", "tap_action", "item_tap_action"),
    ],
)
def test_advice_does_not_rename_options_with_different_semantics(
    definitions: cd.CardDefinitions, card_type: str, key: str, other: str
) -> None:
    """An option the editor does not list is reported as not listed, even when
    the editor lists a similarly named option that means something else."""
    cards = [{"type": card_type, key: "x"}, {"type": card_type, other: "x"}]

    warnings = definitions.validate({"views": [{"cards": cards}]})

    assert [w for w in warnings if "not listed" in w] == [
        f"views[0].cards[0] ({card_type}): editor schema advisory; runtime "
        f"support may differ: '{key}' is not listed in the {card_type} editor schema"
    ]
