"""Ignored config entries: no options or reconfigure flow, read or written.

Core never sets an ignored entry up, so its flows can raise, while Core's
``supports_options`` and ``supports_reconfigure`` never look at ``source``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_entry_flow import update_config_entry_options
from ha_mcp.tools.config_entry_identity import check_reconfigure_identity
from ha_mcp.tools.ignored_entries import normalize_ignored_entry

_FLAGS = {"supports_options": True, "supports_reconfigure": True}


@pytest.mark.parametrize(
    ("source", "expected"), [("ignore", False), ("user", True)], ids=["ignored", "user"]
)
def test_only_an_ignored_entry_loses_its_flow_flags(
    source: str, expected: bool
) -> None:
    entry: dict[str, Any] = {"source": source, **_FLAGS}

    normalize_ignored_entry(entry)

    assert entry["supports_options"] is expected
    assert entry["supports_reconfigure"] is expected


def _client(source: str) -> AsyncMock:
    client = AsyncMock()
    client.get_config_entry.return_value = {
        "entry_id": "e1",
        "domain": "unifi",
        "source": source,
        **_FLAGS,
    }
    return client


@pytest.mark.asyncio
async def test_reconfigure_of_an_ignored_entry_is_refused() -> None:
    client = _client("ignore")

    with pytest.raises(ToolError) as exc_info:
        await check_reconfigure_identity(client, "e1", {})

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "ignored" in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ignore", "user"])
async def test_options_write_starts_a_flow_only_for_an_entry_that_is_not_ignored(
    source: str,
) -> None:
    client = _client(source)
    client.start_options_flow.return_value = {}

    with pytest.raises(ToolError) as exc_info:
        await update_config_entry_options(client, "e1", {"host": "h"})

    if source == "ignore":
        assert "ignored" in json.loads(str(exc_info.value))["error"]["message"]
        client.start_options_flow.assert_not_called()
    else:
        client.start_options_flow.assert_awaited_once_with("e1")
