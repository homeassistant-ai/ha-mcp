"""Warning priority and coverage regressions on a real card-definition index."""

from pathlib import Path
from typing import Any

import pytest

from ._card_definitions_core import card_file, cc, cd
from ._card_definitions_core import custom_cards as custom_cards
from ._card_definitions_core import definitions as definitions


@pytest.fixture
def broken_card(tmp_path: Path, custom_cards: cc.CustomCards) -> cc.CustomCards:
    custom_cards.refresh(
        [
            card_file(
                tmp_path / "button.js",
                "button-card",
                "throw new TypeError('missing panel');",
            )
        ]
    )
    return custom_cards


def test_inconclusive_notices_cannot_displace_schema_findings(
    definitions: cd.CardDefinitions, broken_card: cc.CustomCards
) -> None:
    cards = (
        [{"type": "custom:button-card"}] * 7
        + [{"type": "tile", "entity": "light.test", "colour": "red"}] * 19
        + [{"type": "tyle"}]
    )
    warnings = definitions.validate({"views": [{"cards": cards}]}, broken_card)
    assert sum("'colour'" in warning for warning in warnings) == 19
    assert "unknown card type 'tyle'" in warnings[0]
    assert warnings[-1] == "...and 1 more card diagnostic"


def test_identical_inconclusive_notices_are_grouped_without_losing_the_error(
    definitions: cd.CardDefinitions, broken_card: cc.CustomCards
) -> None:
    warnings = definitions.validate(
        {"views": [{"cards": [{"type": "custom:button-card"}] * 25}]}, broken_card
    )
    assert len(warnings) == 1
    assert "25 cards" in warnings[0] and "views[0].cards[0]" in warnings[0]
    assert "TypeError: missing panel" in warnings[0] and "inconclusive" in warnings[0]


def test_unchecked_card_count_survives_warning_cap(
    definitions: cd.CardDefinitions,
    broken_card: cc.CustomCards,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    check = broken_card.check

    def slow_check(tag: str, config: dict[str, Any]) -> list[dict[str, str]] | None:
        nonlocal elapsed
        result = check(tag, config)
        elapsed += cd.CUSTOM_WAIT_SECONDS + 1
        return result

    monkeypatch.setattr(cd.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(broken_card, "check", slow_check)
    cards = [{"type": "tyle"}] * 21 + [{"type": "custom:button-card"}] * 3
    warnings = definitions.validate({"views": [{"cards": cards}]}, broken_card)
    assert warnings[-1] == "2 custom cards not checked (time budget)"
    assert any("more card diagnostics" in warning for warning in warnings)
