"""Guards a python_transform can use to refuse an edit (#2995)."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_config_automations import AutomationConfigTools, _Target
from ha_mcp.utils.python_sandbox import (
    PythonSandboxExecutionError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
    validate_expression,
)


def _actions(*aliases: str) -> dict[str, Any]:
    return {"actions": [{"alias": a, "data": {"v": 1}} for a in aliases]}


class TestGuards:
    """#2995: a transform can require one match or a value before it edits."""

    ONE_MATCH = "[a] = [a for a in config['actions'] if a.get('alias') == 'Y']"
    VALUE = "[a] = [a for a in [config['actions'][2]] if a['data']['v'] == 1]"

    @pytest.mark.parametrize("guard", [ONE_MATCH, VALUE])
    def test_guard_is_documented(self, guard: str) -> None:
        assert guard in get_security_documentation()

    @pytest.mark.parametrize("guard", [ONE_MATCH, VALUE])
    def test_passing_guard_binds_the_item_to_edit(self, guard: str) -> None:
        result = safe_execute(f"{guard}\na['data']['v'] = 2", _actions("X", "X", "Y"))

        assert result["actions"][2]["data"]["v"] == 2

    @pytest.mark.parametrize(
        ("guard", "config"),
        [
            pytest.param(ONE_MATCH, _actions("X"), id="no-match"),
            pytest.param(ONE_MATCH, _actions("Y", "Y"), id="two-matches"),
            pytest.param(
                "[a] = (a for a in config['actions'] if a.get('alias') == 'Y')",
                _actions("Y", "Y"),
                id="two-matches-no-length",
            ),
        ],
    )
    def test_failed_guard_raises_and_says_why(
        self, guard: str, config: dict[str, Any]
    ) -> None:
        with pytest.raises(PythonSandboxExecutionError, match="unpack") as exc_info:
            safe_execute(guard, config)

        _, suggestions = format_sandbox_error(exc_info.value, guard)
        assert suggestions[0].startswith("A single-target unpack needs exactly one")
        assert "guard, it matched no item or several" in suggestions[0]

    def test_multi_target_unpack_error_gets_no_guard_hint(self) -> None:
        expr = "x, y = config['actions']"

        with pytest.raises(PythonSandboxExecutionError, match="unpack") as exc_info:
            safe_execute(expr, _actions("X"))

        _, suggestions = format_sandbox_error(exc_info.value, expr)
        assert not any("guard" in s for s in suggestions)

    @pytest.mark.anyio
    async def test_failed_guard_writes_nothing(self, monkeypatch) -> None:
        client = AsyncMock()
        tools = AutomationConfigTools(client)

        async def get_config(_: str) -> tuple[dict[str, Any], str, _Target]:
            return _actions("X"), "hash", _Target("id", None, None)

        monkeypatch.setattr(tools, "_get_automation_config_internal", get_config)

        with pytest.raises(ToolError, match="unpack"):
            await tools._run_python_transform(
                "id", "hash", self.ONE_MATCH, None, False, None, False
            )

        client.upsert_automation_config.assert_not_awaited()

    def test_assert_rejection_points_at_guards(self) -> None:
        valid, error = validate_expression("assert config")

        assert valid is False
        assert "unpack a filtered list" in error
