"""Regression cases for production routing on Core's refusal codes."""

from pathlib import Path

import pytest

from ._rule_scan import RULES, scan


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("""if "not found" in str(err).lower(): missing()""", True),
        (
            """if code == "not_found" or "not found" in error_msg.lower(): missing()""",
            True,
        ),
        (
            """if core_error_message(reply.get("error", "")) == "Label ID doesn't exist": missing()""",
            True,
        ),
        ("""if code == "not_found": missing()""", False),
        ("""if code == "unknown_error" and reason == "No data": explain()""", False),
        ("""logger.info("not found: %s", err)""", False),
    ],
)
@pytest.mark.parametrize("tree", ["src/ha_mcp", "custom_components/ha_mcp_tools"])
def test_classification_rule(
    tmp_path: Path, tree: str, source: str, expected: bool
) -> None:
    sample = Path(tree, "sample.py")
    path = tmp_path / sample
    path.parent.mkdir(parents=True)
    path.write_text(source + "\n", encoding="utf-8")
    matches = scan(
        RULES / "no-error-text-classification.yml", tmp_path, sample.as_posix()
    )
    assert bool(matches) is expected
