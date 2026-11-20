"""Which tool calls the security-policy gate holds, and which it lets through.

Each test drives ``PolicyMiddleware.on_call_tool`` with FastMCP's own call
context. The downstream tool is a recorder: whether it runs is the property.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PROXY_META_TOOLS
from ha_mcp.policy.model import Policy, Predicate, Rule
from ha_mcp.policy.persistence import POLICY_FILENAME, load_policy

from ._middleware_core import error_body, gate, tool_call

WILDCARD = Policy(rules=[Rule(tool_name="*")])


async def test_proxy_meta_tools_pass_through() -> None:
    """The proxy tools only route to a real tool, which the gate sees itself."""
    mw = gate(WILDCARD, ApprovalQueue())
    call_next = AsyncMock(return_value="proxy_result")

    for name in PROXY_META_TOOLS:
        assert await mw.on_call_tool(tool_call(name), call_next) == "proxy_result"


async def test_no_matching_rule_passes_through() -> None:
    mw = gate(Policy(rules=[Rule(tool_name="ha_other")]), ApprovalQueue())
    call_next = AsyncMock(return_value="ok")

    assert await mw.on_call_tool(tool_call("ha_call_service"), call_next) == "ok"
    call_next.assert_awaited_once()


async def test_a_rule_gates_a_call_on_the_tools_retired_name() -> None:
    """The gate is keyed on the current name; ``evaluate`` fails OPEN.

    The alias middleware normally rewrites the name before this gate sees it,
    so this pins the defence rather than the ordering: reading the raw name
    here means no rule matches, and an unmatched call is allowed.
    """
    mw = gate(Policy(rules=[Rule(tool_name="ha_manage_app")]), ApprovalQueue())
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_manage_addon", {"slug": "x"}), call_next)

    assert error_body(ei.value)["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_called()


async def test_stringified_selector_argument_still_gates_correctly() -> None:
    """A JSON-stringified `selector` (Claude Desktop stdio-style) must still
    be gated by a rule targeting a nested selector field.

    The gate reads the arguments before the tool's own Pydantic coercion
    runs (inside call_next), so without normalizing here a rule scoped to
    ``args.selector.domain == "lock"`` would never match a lock selector sent
    as a string, and the call would run.
    """
    policy = Policy(
        rules=[
            Rule(
                tool_name="ha_bulk_control",
                when=[Predicate(path="args.selector.domain", op="eq", value="lock")],
            )
        ]
    )
    call_next = AsyncMock()
    stringified_args = {
        "selector": '{"domain": "lock", "area_ids": ["entry"]}',
        "action": "lock",
    }

    with pytest.raises(ToolError) as ei:
        await gate(policy, ApprovalQueue()).on_call_tool(
            tool_call("ha_bulk_control", stringified_args), call_next
        )

    assert error_body(ei.value)["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_awaited()


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("{not json", id="corrupt-json"),
        # Issue #2540: read as a require-approval list, it let everything else run.
        pytest.param(
            json.dumps(
                {"rule_efect": "allow", "rules": [{"tool_name": "ha_get_state"}]}
            ),
            id="misspelled-key",
        ),
    ],
)
async def test_an_unreadable_policy_file_fails_closed(
    tmp_path: Path, stored: str
) -> None:
    """A policy file the server cannot read blocks calls instead of passing them.

    Silently allowing every call while the user's rules sit unparsed on disk
    would be the wrong default for a security feature.
    """
    (tmp_path / POLICY_FILENAME).write_text(stored)
    mw = gate(lambda: load_policy(tmp_path), ApprovalQueue())
    call_next = AsyncMock(return_value="should_not_run")

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            tool_call("ha_call_service", {"domain": "lock"}), call_next
        )

    assert error_body(ei.value)["error"]["code"] == "POLICY_LOAD_FAILED"
    call_next.assert_not_called()


async def test_deeply_nested_args_fail_closed_not_silently_allowed() -> None:
    """A RecursionError while normalizing args must fail the call closed.

    Evaluating (and hashing) the unnormalized args instead would let a rule
    scoped to a nested selector field never match the deep value, landing on
    ALLOW where a shallower version of the same call needs approval.
    """
    mw = gate(Policy(rules=[Rule(tool_name="ha_call_service")]), ApprovalQueue())
    call_next = AsyncMock(return_value="should_not_run")

    nested: object = "leaf"
    for _ in range(sys.getrecursionlimit() + 100):
        nested = {"nested": nested}

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            tool_call("ha_call_service", {"domain": "lock", "data": nested}),
            call_next,
        )

    assert error_body(ei.value)["error"]["code"] == "POLICY_ARGS_TOO_DEEPLY_NESTED"
    call_next.assert_not_called()


@pytest.mark.parametrize("action", ["list_pending", "approve", "deny"])
async def test_queue_management_action_passes_through(action: str) -> None:
    """Deciding the queue over MCP must not itself wait for a decision.

    Gating "approve" under a wildcard rule would mint a second pending entry
    instead of deciding the first, and an MCP-only approval workflow would
    deadlock (Codex #1993 P1). Each action is one the workflow needs.
    """
    call_next = AsyncMock(return_value="passed")

    result = await gate(WILDCARD, ApprovalQueue()).on_call_tool(
        tool_call("ha_dev_manage_server", {"action": action, "token": "t"}),
        call_next,
    )

    assert result == "passed"
    call_next.assert_awaited_once()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param("ha_dev_manage_server", {"action": "restart"}, id="other-action"),
        pytest.param(
            "ha_dev_manage_settings",
            {"action": "approve", "token": "t"},
            id="other-tool",
        ),
        pytest.param("ha_dev_manage_server", {}, id="no-action"),
    ],
)
async def test_non_management_call_stays_gated(
    name: str, arguments: dict[str, str]
) -> None:
    """Only the queue's own decisions skip the gate, not the rest of the tool."""
    call_next = AsyncMock(return_value="should_not_run")

    with pytest.raises(ToolError) as ei:
        await gate(WILDCARD, ApprovalQueue()).on_call_tool(
            tool_call(name, arguments), call_next
        )

    assert error_body(ei.value)["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_called()
