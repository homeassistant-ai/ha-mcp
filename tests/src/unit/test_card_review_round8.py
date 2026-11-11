"""Warning priority and coverage regressions on a real card-definition index."""

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

cd = importlib.import_module("custom_components.ha_mcp_tools.card_definitions")

# A frontend build with one validated card: ``tile`` has an editor whose struct
# lists the keys ``type`` and ``entity`` and rejects any other key as ``never``.
_FRONTEND = (
    "export const __webpack_modules__={"
    '8(e){(0,e.EM)("hui-tile-card");class A{setConfig(c){}}},'
    '9(e){(0,e.EM)("hui-entities-card");class B{setConfig(c){}}},'
    '10(m,e,r){(0,e.EM)("hui-tile-card-editor");var s=r(11),'
    "v=(0,s.object)({type:1,entity:1});class C{setConfig(c){(0,s.assert)(c,v)}}},"
    "11(m,e,r){r.d(e,{},{object:(schema)=>({schema,validate:(c)=>{"
    "const f=Object.keys(c).filter((k)=>!(k in schema)).map("
    '(k)=>({path:[k],type:"never",message:"never"}));'
    "return f.length?[{failures:()=>f}]:[undefined,c]}})})}};"
)


def _definitions(root: Path) -> Any:
    frontend = root / "frontend_latest"
    frontend.mkdir()
    (frontend / "cards.js").write_text(_FRONTEND, encoding="utf-8")
    return cd.CardDefinitions(root)


def test_inconclusive_notices_cannot_displace_schema_findings(tmp_path: Path) -> None:
    definitions = _definitions(tmp_path)
    custom = MagicMock()
    custom.check.return_value = [
        {"source": "inspection", "message": "TypeError: missing panel"}
    ]
    cards = (
        [{"type": "custom:button-card"}] * 7
        + [{"type": "tile", "colour": "red"}] * 19
        + [{"type": "tyle"}]
    )
    warnings = definitions.validate({"views": [{"cards": cards}]}, custom)
    assert sum("'colour'" in warning for warning in warnings) == 19
    assert "unknown card type 'tyle'" in warnings[0]
    assert warnings[-1] == "...and 1 more card diagnostic"


def test_identical_inconclusive_notices_are_grouped_without_losing_the_error(
    tmp_path: Path,
) -> None:
    definitions = _definitions(tmp_path)
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


def test_unchecked_card_count_survives_warning_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = _definitions(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: clock[0])
    custom = MagicMock()

    def check(*args):
        # One check outlasts any custom-card time budget.
        clock[0] += 3600
        return [{"source": "card", "message": "needs entity"}]

    custom.check.side_effect = check
    cards = [{"type": "tyle"}] * 21 + [{"type": "custom:example"}] * 3
    warnings = definitions.validate({"views": [{"cards": cards}]}, custom)
    assert custom.check.call_count == 1
    assert warnings[-1] == "2 custom cards not checked (time budget)"
    assert any("more card diagnostics" in warning for warning in warnings)
