"""The rule against typing in a server function's answer
(``rules/no-typed-in-server-answers.yml``) refuses the forms it names and
nothing else, and no test trips it."""

from pathlib import Path

import pytest

from ._rule_scan import REPO, RULES, flagged, scan

_SERVER_ANSWERS = "no-typed-in-server-answers"


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            'patch("ha_mcp.tools.x.get_component_caps", return_value={"caps": []})\n',
            True,
            id="patch-return-value",
        ),
        pytest.param(
            'patch.object(update_check, "get_update_info", AsyncMock(return_value=None))\n',
            True,
            id="patch-object-mock",
        ),
        pytest.param(
            'monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", lambda *_: [])\n',
            True,
            id="setattr-lambda",
        ),
        pytest.param(
            "patch.multiple(update_check, get_update_info=AsyncMock(return_value=None))\n",
            True,
            id="patch-multiple-keyword",
        ),
        pytest.param(
            "patch.multiple(update_check, get_update_info=DEFAULT)\n",
            True,
            id="patch-multiple-default-mock",
        ),
        pytest.param(
            "patch.multiple(\n"
            "    mod, get_update_info=mod.get_update_info, helper=Mock(return_value=None)\n"
            ")\n",
            False,
            id="patch-multiple-answer-on-another-keyword",
        ),
        pytest.param(
            'with patch("ha_mcp.tools.component_api.get_component_caps") as caps:\n'
            '    caps.return_value = {"available": True}\n',
            True,
            id="bare-patch-answered-through-its-alias",
        ),
        pytest.param(
            'with patch.object(update_check, "get_update_info") as info:\n'
            "    info.return_value = None\n",
            True,
            id="bare-patch-object",
        ),
        pytest.param(
            'patch.object(tools_addons, "get_addon_info", wraps=tools_addons.get_addon_info)\n',
            False,
            id="spy",
        ),
        pytest.param(
            'patch.object(update_check, "fetch_latest", side_effect=TimeoutError)\n',
            False,
            id="failure",
        ),
        pytest.param(
            'patch.object(paths, "get_data_dir", return_value=tmp_path)\n',
            False,
            id="another-function",
        ),
        pytest.param(
            'patch.object(renderer, "label", return_value="get_update_info")\n',
            False,
            id="another-function-returning-a-listed-name",
        ),
        pytest.param(
            'patch.object(renderer, "label", new=Mock(get_update_info=lambda: 1))\n',
            False,
            id="another-function-with-a-listed-keyword-inside",
        ),
        pytest.param(
            'monkeypatch.setattr("ha_mcp.update_check.get_update_info", lambda: None)\n',
            True,
            id="setattr-dotted-target",
        ),
        pytest.param(
            'patch("ha_mcp.update_check.get_update_info", wraps=lambda: real_get_update_info())\n',
            False,
            id="spy-lambda-that-calls-through",
        ),
        pytest.param(
            'patch("ha_mcp.update_check.get_update_info", wraps=lambda: dict(latest="1"))\n',
            True,
            id="spy-lambda-calling-something-else",
        ),
        pytest.param(
            'patch("ha_mcp.update_check.get_update_info", wraps=lambda: {"latest": "1"})\n',
            True,
            id="spy-lambda-with-a-typed-in-answer",
        ),
        pytest.param(
            'patch("ha_mcp.update_check.get_update_info", side_effect=lambda: {"latest": "1"})\n',
            True,
            id="side-effect-lambda",
        ),
    ],
)
def test_only_a_typed_in_server_answer_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    assert flagged(_SERVER_ANSWERS, source, tmp_path) is refused


def test_no_test_types_in_a_server_answer() -> None:
    """The rule has no list of exempt files: every hit is a violation."""
    rows = scan(RULES / f"{_SERVER_ANSWERS}.yml", REPO, "tests")

    assert sorted({row["file"].replace("\\", "/") for row in rows}) == []
