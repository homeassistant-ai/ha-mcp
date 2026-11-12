"""What sandbox code gets back from ``call_tool`` in ``ha_manage_custom_tool``.

FastMCP hands the bridge a ToolResult of content blocks; Monty can only pass
basic Python types into the sandbox, so the bridge decodes them. Naked list
returns must reach the sandbox as iterable lists, not as ``str(list)`` reprs
(the M3 fix from the round-3 review): sandbox code commonly does
``for item in result:``, which breaks on a string. Each case runs real sandbox
code that calls a real tool registered beside the custom-tool tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool, ToolResult
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_code import register_code_tools


@pytest.fixture
def code_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """ha_manage_custom_tool registers only with (beta) code mode on."""
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("ENABLE_CODE_MODE", "true")
    reset_global_settings()
    yield
    reset_global_settings()


async def _sandbox_sees(client: HomeAssistantClient, probe: Callable[[], Any]) -> Any:
    """What ``await call_tool("probe", {})`` evaluates to inside the sandbox."""
    mcp = FastMCP("sandbox-results")
    mcp.add_tool(Tool.from_function(probe, name="probe"))
    register_code_tools(mcp, client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "ha_manage_custom_tool",
            {
                "code": 'await call_tool("probe", {})',
                "justification": "reads another tool's result",
            },
        )
    assert isinstance(result.structured_content, dict)
    return result.structured_content["data"]["result"]


def _list_of_dicts() -> list[dict[str, Any]]:
    # The example Patch76 called out in the round-3 review.
    return [{"id": 1}, {"id": 2}]


def _data_rows() -> list[dict[str, str]]:
    # Rows without ``text``/``type`` keys are data, not content blocks.
    return [{"entity_id": "light.foo", "state": "on"}]


def _dict() -> dict[str, Any]:
    return {"key": "value", "n": 1}


def _plain_text() -> str:
    return "plain text"


@pytest.mark.usefixtures("code_mode")
@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        pytest.param(_list_of_dicts, [{"id": 1}, {"id": 2}], id="list-of-dicts"),
        pytest.param(
            _data_rows, [{"entity_id": "light.foo", "state": "on"}], id="data-rows"
        ),
        pytest.param(_dict, {"key": "value", "n": 1}, id="json-object"),
        pytest.param(_plain_text, "plain text", id="non-json-text"),
    ],
)
async def test_sandbox_code_gets_the_tool_result_as_basic_python_values(
    core_client: HomeAssistantClient, probe: Callable[[], Any], expected: Any
) -> None:
    assert await _sandbox_sees(core_client, probe) == expected


@pytest.mark.usefixtures("code_mode")
async def test_a_tool_error_reaches_sandbox_code_as_an_error_dict(
    core_client: HomeAssistantClient,
) -> None:
    """Sandbox code branches on ``result.get("error")`` as it does for
    ``api_get``/``api_post``/``ws_send`` failures."""

    def _failing() -> ToolResult:
        return ToolResult(content=json.dumps({"detail": "boom"}), is_error=True)

    seen = await _sandbox_sees(core_client, _failing)

    assert isinstance(seen, dict)
    assert "boom" in seen["error"]
