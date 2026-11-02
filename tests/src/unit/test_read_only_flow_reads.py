"""Read Only Mode: read tools open no config-entry flow.

A flow's first step, or a menu choice, can finish the flow, and Core saves
the options or a subentry before it answers. So in Read Only Mode the
describes that start a flow are refused, and the reads that would probe the
options flow leave the options out and say why.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.read_only import read_only_request
from ha_mcp.tools.integration_schema import redact_component_options
from ha_mcp.tools.smart_search._deep import DeepSearchMixin
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)


def _tools() -> IntegrationTools:
    tools = IntegrationTools(AsyncMock())
    tools._get_entry_detail_response = AsyncMock(return_value={"success": True})  # type: ignore[method-assign]
    tools._list_entries = AsyncMock(return_value={"success": True, "entries": []})  # type: ignore[method-assign]
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        {"entry_id": "e1", "include_schema": True},
        {"entry_id": "e1", "include_schema": True, "menu_choice": "advanced"},
        {
            "entry_id": "e1",
            "include_subentry_schema": True,
            "subentry_type": "conversation",
        },
        {
            "entry_id": "e1",
            "include_subentry_schema": True,
            "subentry_type": "conversation",
            "menu_choice": "advanced",
        },
    ],
    ids=["options", "options-menu", "subentry", "subentry-menu"],
)
async def test_read_only_refuses_an_entry_flow_describe(call: dict[str, Any]) -> None:
    tools = _tools()

    with read_only_request(), pytest.raises(ToolError) as exc_info:
        await tools.ha_get_integration(**call)

    assert json.loads(str(exc_info.value))["error"]["code"] == "READ_ONLY_MODE"
    tools._get_entry_detail_response.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "read"),
    [
        ({"entry_id": "e1"}, "_get_entry_detail_response"),
        ({"domain": "demo"}, "_list_entries"),
        ({"include_schema": True}, "_list_entries"),
        ({"domain": "demo", "include_schema": True}, "_list_entries"),
    ],
    ids=["entry", "list-domain", "schema-without-entry", "schema-with-domain"],
)
async def test_read_only_still_allows_the_reads_that_start_no_describe(
    call: dict[str, Any], read: str
) -> None:
    tools = _tools()

    with read_only_request():
        await tools.ha_get_integration(**call)

    getattr(tools, read).assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_options_probe_opens_no_flow() -> None:
    client = AsyncMock()

    with read_only_request():
        result = await fetch_entry_options_with_status(client, "e1")

    assert result == ({}, False)
    client.start_options_flow.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_names_itself_as_why_options_are_missing() -> None:
    tools = IntegrationTools(AsyncMock())
    entry: dict[str, Any] = {"supports_options": True}
    warnings: list[str] = []

    with read_only_request():
        await tools._probe_legacy_entry_options(entry, "e1", None, warnings)

    assert entry["options"] == {}
    assert "Read Only Mode" in warnings[0]


def test_read_only_names_itself_on_the_list_and_in_ha_search() -> None:
    tools = IntegrationTools(AsyncMock())
    response: dict[str, Any] = {"success": True}

    with read_only_request():
        listed = tools._finalize_entry_list([], None, None, None, 50, 0, ["e1"])
        DeepSearchMixin._apply_per_type_partial_flag(response, helper_failed=1)

    assert "Read Only Mode" in listed["warnings"][0]
    assert "Read Only Mode" in response["partial_reason"]


@pytest.mark.asyncio
async def test_read_only_redacts_component_options_without_a_probe() -> None:
    client = AsyncMock()
    entry: dict[str, Any] = {"supports_options": True, "options": {"host": "h"}}
    warnings: list[str] = []

    with read_only_request():
        await redact_component_options(client, "e1", entry, warnings)

    client.start_options_flow.assert_not_called()
    assert entry["options"]["host"] != "h"
    assert "no options schema readable" in warnings[0]
    assert "Read Only Mode" in warnings[0]
