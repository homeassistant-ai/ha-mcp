"""E2E: home context documents stored by the component in Home Assistant.

Covers the write/read/conflict/restore cycle against the component's storage
and the reminder a write tool's result carries when a document covers its
target. Lanes without an active component entry pin the actionable error.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.topology import component_surface_available


def _scope() -> str:
    return f"topic:e2e_{uuid.uuid4().hex[:8]}"


async def _set(mcp: MCPAssertions, scope: str, expected: int, content: str) -> Any:
    return await mcp.call_tool_success(
        "ha_manage_home_context",
        {
            "action": "set",
            "scope": scope,
            "expected_revision": expected,
            "content": content,
        },
    )


async def test_home_context_lifecycle(mcp_client: Any) -> None:
    scope = _scope()
    async with MCPAssertions(mcp_client) as mcp:
        if not component_surface_available():
            await mcp.call_tool_failure(
                "ha_get_home_context", {}, expected_error="COMPONENT_NOT_INSTALLED"
            )
            return
        try:
            created = await _set(mcp, scope, 0, "# Heating\nNever below 19 °C.")
            assert created["revision"] == 1

            stale = await mcp.call_tool_failure(
                "ha_manage_home_context",
                {
                    "action": "append",
                    "scope": scope,
                    "expected_revision": 0,
                    "content": "lost update",
                },
            )
            assert stale.get("latest_revision") == 1

            await mcp.call_tool_success(
                "ha_manage_home_context",
                {
                    "action": "replace_text",
                    "scope": scope,
                    "expected_revision": 1,
                    "find": "19 °C",
                    "content": "20 °C",
                },
            )
            await mcp.call_tool_success(
                "ha_manage_home_context",
                {
                    "action": "restore",
                    "scope": scope,
                    "expected_revision": 2,
                    "revision": 1,
                },
            )
            current = await mcp.call_tool_success(
                "ha_get_home_context", {"scope": scope, "include_history": True}
            )
            assert current["latest_revision"] == 3
            assert current["document"]["content"].endswith("Never below 19 °C.")
            assert len(current["history"]) == 3

            listing = await mcp.call_tool_success("ha_get_home_context", {})
            assert scope in [row["scope"] for row in listing["scopes"]]
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_manage_home_context",
                {"action": "delete", "scope": scope, "expected_revision": 3},
            )


async def test_a_write_on_a_described_entity_reminds_the_agent(
    mcp_client: Any,
) -> None:
    # Without the component there is nothing to remind of; the lifecycle test
    # above pins that lane's error.
    if not component_surface_available():
        return
    entity_id = "light.bed_light"
    scope = f"entity:{entity_id}"
    async with MCPAssertions(mcp_client) as mcp:
        before = await mcp.call_tool_success("ha_get_home_context", {"scope": scope})
        revision = before["latest_revision"]
        try:
            await _set(mcp, scope, revision, "Bedside lamp; dim after 22:00.")
            result = await mcp.call_tool_success(
                "ha_call_service",
                {
                    "domain": "homeassistant",
                    "service": "update_entity",
                    "entity_id": entity_id,
                },
            )
            warnings = result.get("warnings") or []
            assert any(f"{scope} (revision {revision + 1})" in w for w in warnings), (
                warnings
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_manage_home_context",
                {
                    "action": "delete",
                    "scope": scope,
                    "expected_revision": revision + 1,
                },
            )
