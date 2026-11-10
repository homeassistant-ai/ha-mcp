"""Shared fixtures and helpers for automation workflow E2E tests."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    looks_like_success,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_entity_registration

logger = logging.getLogger(__name__)


@pytest.fixture
async def trace_entity_cleanup(mcp_client):
    """Yield a list that tracks automations/scripts created by trace tests.

    Every entry is a ``(domain, entity_id)`` tuple. At teardown each tracked
    automation/script is removed with the config tools so tests do not leak
    entities on the shared HA instance. The fixture is per test: an entry a
    test removes itself (a blueprint's automation, which must go before its
    blueprint) is dropped from the list, and the teardown still covers the
    rest.
    """
    created: list[tuple[str, str]] = []
    yield created

    logger.info(f"🧹 Cleaning up {len(created)} trace test entities")
    failed: list[tuple[str, str]] = []
    for domain, entity_id in created:
        if domain == "automation":
            result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_automation",
                {"entity_id": entity_id},
            )
            if not looks_like_success(result):
                failed.append((domain, entity_id))
        elif domain == "script":
            script_id = entity_id.split(".", 1)[-1]
            result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_script",
                {"script_id": script_id},
            )
            if not looks_like_success(result):
                failed.append((domain, entity_id))
        else:
            logger.warning(f"⚠️ Unknown trace entity domain: {domain} {entity_id}")
    if failed:
        logger.warning(f"⚠️ Failed to remove {len(failed)} trace entities: {failed}")


async def create_trace_automation(
    mcp_client, cleanup: list[tuple[str, str]], config: dict[str, Any]
) -> str:
    """Create an automation and wait until its entity is queryable.

    The ``(\"automation\", entity_id)`` pair is appended to ``cleanup`` on
    success. If creation resolves without an entity id, the leftover config is
    removed before failing.
    """
    create_data = await MCPAssertions(mcp_client).call_tool_success(
        "ha_config_set_automation", {"config": config}
    )
    automation_id = create_data.get("entity_id")
    leftover_error = None
    if not automation_id:
        # The config exists even though entity discovery timed out; remove
        # it before failing so it cannot pollute later tests on this HA.
        config_id = create_data.get("automation_id")
        if config_id:
            remove_result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_automation",
                {"entity_id": config_id},
            )
            if not looks_like_success(remove_result):
                cleanup.append(("automation", config_id))
            leftover_error = remove_result.get("error")
    assert automation_id, (
        f"ha_config_set_automation did not resolve an entity id: {create_data}"
        + (
            f"; removing the leftover config also failed: {leftover_error}"
            if leftover_error
            else ""
        )
    )
    cleanup.append(("automation", automation_id))
    if not await wait_for_entity_registration(mcp_client, automation_id):
        remove_result = await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"entity_id": automation_id},
        )
        if looks_like_success(remove_result):
            cleanup.pop()
        raise AssertionError(f"{automation_id} never became queryable")
    return automation_id


async def create_trace_script(
    mcp_client,
    cleanup: list[tuple[str, str]],
    script_id: str,
    config: dict[str, Any],
) -> str:
    """Create a script and wait until its entity is queryable.

    The ``(\"script\", entity_id)`` pair is appended to ``cleanup`` on success.
    """
    create_data = await MCPAssertions(mcp_client).call_tool_success(
        "ha_config_set_script", {"script_id": script_id, "config": config}
    )
    script_entity_id = create_data.get("entity_id") or f"script.{script_id}"
    cleanup.append(("script", script_entity_id))
    assert await wait_for_entity_registration(mcp_client, script_entity_id), (
        f"{script_entity_id} never became queryable"
    )
    return script_entity_id
