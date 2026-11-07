"""Read Only Mode: the integration reads still open the options flow (first form only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.read_only import read_only_request
from ha_mcp.tools.smart_search._deep import DeepSearchMixin
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

_FORM = {
    "type": "form",
    "flow_id": "f1",
    "step_id": "init",
    "data_schema": [
        {
            "name": "host",
            "selector": {"text": {}},
            "description": {"suggested_value": "h"},
        }
    ],
}


def _tools() -> IntegrationTools:
    tools = IntegrationTools(AsyncMock())
    tools._get_entry_detail_response = AsyncMock(return_value={"success": True})  # type: ignore[method-assign]
    tools._list_entries = AsyncMock(return_value={"success": True, "entries": []})  # type: ignore[method-assign]
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "read"),
    [
        ({"entry_id": "e1"}, "_get_entry_detail_response"),
        ({"entry_id": "e1", "include_schema": True}, "_get_entry_detail_response"),
        (
            {
                "entry_id": "e1",
                "include_subentry_schema": True,
                "subentry_type": "conversation",
            },
            "_get_entry_detail_response",
        ),
        ({"domain": "demo"}, "_list_entries"),
    ],
    ids=["entry", "options-first-form", "subentry-first-form", "list-domain"],
)
async def test_read_only_runs_the_entry_reads_and_first_form_describes(
    call: dict[str, Any], read: str
) -> None:
    tools = _tools()

    with read_only_request():
        await tools.ha_get_integration(**call)

    getattr(tools, read).assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_options_probe_reads_the_options_flow() -> None:
    client = AsyncMock()
    client.start_options_flow.return_value = dict(_FORM)

    with read_only_request():
        options, read = await fetch_entry_options_with_status(client, "e1")

    assert (options, read) == ({"host": "h"}, True)
    client.abort_options_flow.assert_awaited_once_with("f1")


def test_read_only_is_not_named_as_why_a_helper_search_is_partial() -> None:
    response: dict[str, Any] = {"success": True}

    with read_only_request():
        DeepSearchMixin._apply_per_type_partial_flag(response, helper_failed=1)

    assert "OptionsFlow could not be read" in response["partial_reason"]
