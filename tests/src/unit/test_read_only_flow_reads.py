"""Read Only Mode: the integration reads still open the options flow (first form only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.read_only import read_only_request
from ha_mcp.tools.integration_schema import redact_component_options
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


@pytest.mark.asyncio
async def test_read_only_entry_read_fills_options_from_the_options_flow() -> None:
    client = AsyncMock()
    client.start_options_flow.return_value = dict(_FORM)
    tools = IntegrationTools(client)
    entry: dict[str, Any] = {"supports_options": True}
    warnings: list[str] = []

    with read_only_request():
        await tools._probe_legacy_entry_options(entry, "e1", None, warnings)

    assert entry["options"] == {"host": "h"}
    assert warnings == []


def test_read_only_is_not_named_as_why_options_are_missing() -> None:
    tools = IntegrationTools(AsyncMock())
    response: dict[str, Any] = {"success": True}

    with read_only_request():
        listed = tools._finalize_entry_list([], None, None, None, 50, 0, ["e1"])
        DeepSearchMixin._apply_per_type_partial_flag(response, helper_failed=1)

    assert "OptionsFlow could not be read" in listed["warnings"][0]
    assert "OptionsFlow could not be read" in response["partial_reason"]


@pytest.mark.asyncio
async def test_read_only_redaction_probe_reads_the_password_fields() -> None:
    client = AsyncMock()
    client.start_options_flow.return_value = {
        **_FORM,
        "data_schema": [
            {"name": "host", "selector": {"text": {}}},
            {"name": "api_key", "selector": {"text": {"type": "password"}}},
        ],
    }
    entry: dict[str, Any] = {
        "supports_options": True,
        "options": {"host": "h", "api_key": "k"},
    }
    warnings: list[str] = []

    with read_only_request():
        await redact_component_options(client, "e1", entry, warnings)

    assert entry["options"]["host"] == "h"
    assert entry["options"]["api_key"] != "k"
    assert warnings == []
