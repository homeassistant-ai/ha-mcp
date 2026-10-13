"""Warning priority and coverage regressions reproduced on embedded HA."""

from unittest.mock import MagicMock

from .test_card_definitions import _definitions, cd


def test_inconclusive_notices_cannot_displace_schema_findings():
    definitions = _definitions({"tile"}, {"tile": ["type", "entity"]})
    definitions._engine = MagicMock(
        return_value={"value": [[{"type": "never", "path": ["colour"]}]] * 19}
    )
    custom = MagicMock()
    custom.check.return_value = [
        {"source": "inspection", "message": "TypeError: missing panel"}
    ]
    cards = (
        [{"type": "custom:button-card"}] * 7
        + [{"type": "tile"}] * 19
        + [{"type": "tyle"}]
    )
    warnings = definitions.validate({"views": [{"cards": cards}]}, custom)
    assert sum("'colour'" in warning for warning in warnings) == 19
    assert "unknown card type 'tyle'" in warnings[0]
    assert warnings[-1] == "...and 1 more card diagnostic"


def test_identical_inconclusive_notices_are_grouped_without_losing_the_error():
    definitions = _definitions(set(), {})
    custom = MagicMock()
    custom.check.return_value = [
        {"source": "inspection", "message": "TypeError: missing panel"}
    ]
    warnings = definitions.validate(
        {"views": [{"cards": [{"type": "custom:button-card"}] * 25}]}, custom
    )
    assert len(warnings) == 1
    assert "25 cards" in warnings[0] and "views[0].cards[0]" in warnings[0]
    assert "TypeError: missing panel" in warnings[0] and "inconclusive" in warnings[0]


def test_unchecked_card_count_survives_warning_cap(monkeypatch):
    definitions = _definitions(set(), {})
    clock = [0.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: clock[0])
    custom = MagicMock()

    def check(*args):
        clock[0] += cd._CUSTOM_WAIT_SECONDS + 1
        return [{"source": "card", "message": "needs entity"}]

    custom.check.side_effect = check
    cards = [{"type": "tyle"}] * 21 + [{"type": "custom:example"}] * 3
    warnings = definitions.validate({"views": [{"cards": cards}]}, custom)
    assert custom.check.call_count == 1
    assert warnings[-1] == "2 custom cards not checked (time budget)"
    assert any("more card diagnostics" in warning for warning in warnings)
