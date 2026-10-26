"""Disposable objects and MCP-only observations for continuity scenarios."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ..utilities.assertions import MCPAssertions, safe_call_tool
from ..utilities.wait_helpers import wait_for_tool_result

_LOGGER = logging.getLogger(__name__)


class Scenario:
    def __init__(self, client: Any, mcp: MCPAssertions) -> None:
        self.client = client
        self.mcp = mcp
        self.cleanup: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def unique_name() -> str:
        return f"continuity_{uuid.uuid4().hex}"

    def remove_after(self, tool: str, **arguments: Any) -> None:
        self.cleanup.append((tool, arguments))

    async def helper(self, helper_type: str, initial: str | bool) -> str:
        created = await self.mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": self.unique_name(),
                "initial": initial,
            },
        )
        entity_id = created.get("entity_id") or f"{helper_type}.{created['data']['id']}"
        self.remove_after(
            "ha_remove_helpers_integrations",
            helper_type=helper_type,
            target=entity_id,
            confirm=True,
        )
        await wait_for_tool_result(
            self.client,
            "ha_get_state",
            {"entity_id": entity_id},
            lambda result: result.get("data", {}).get("entity_id") == entity_id,
            description=f"new helper {entity_id} is queryable",
        )
        return entity_id

    async def script(self, config: dict[str, Any]) -> str:
        script_id = self.unique_name()
        self.remove_after("ha_config_remove_script", script_id=script_id)
        await self.mcp.call_tool_success(
            "ha_config_set_script", {"script_id": script_id, "config": config}
        )
        await wait_for_tool_result(
            self.client,
            "ha_get_state",
            {"entity_id": f"script.{script_id}"},
            lambda result: (
                result.get("data", {}).get("entity_id") == f"script.{script_id}"
            ),
            description=f"new script {script_id} is queryable",
        )
        return script_id

    async def state(self, entity_id: str, expected: str) -> None:
        await wait_for_tool_result(
            self.client,
            "ha_get_state",
            {"entity_id": entity_id},
            lambda result: result["data"]["state"] == expected,
            description=f"{entity_id} reaches {expected!r}",
        )


@asynccontextmanager
async def public_scenario(client: Any) -> AsyncIterator[Scenario]:
    async with MCPAssertions(client) as mcp:
        scenario = Scenario(client, mcp)
        try:
            yield scenario
        finally:
            failing = sys.exception() is not None
            errors = []
            for tool, arguments in reversed(scenario.cleanup):
                try:
                    result = await safe_call_tool(client, tool, arguments)
                    if result.get("success") is not True:
                        errors.append((tool, result))
                except Exception as error:
                    _LOGGER.exception("Continuity cleanup failed: %s", tool)
                    errors.append((tool, str(error)))
            if errors:
                _LOGGER.error("Continuity cleanup failures: %s", errors)
                if not failing:
                    raise AssertionError(f"Owned objects were not removed: {errors}")
