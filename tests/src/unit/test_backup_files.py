"""A file snapshot keeps line endings as stored when the component can.

Kept out of ``test_backup_manager.py``, which is at its module-size baseline.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp import backup_files, backup_manager
from ha_mcp.tools import component_api, tools_filesystem


def _caps(*capabilities: str) -> component_api.ComponentCaps:
    return component_api.ComponentCaps(
        schema_version=1,
        component_version="9.9.9",
        capabilities=frozenset(capabilities),
        limits={},
    )


async def _snapshot(caps: Any) -> tuple[Any, AsyncMock]:
    service = AsyncMock(return_value={"success": True, "content": "a\r\nb\r\n"})
    with (
        patch.object(component_api, "get_component_caps", AsyncMock(return_value=caps)),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
    ):
        content = await backup_files._fetch_file_as_stored(MagicMock(), "www/a.css")
    return content, service


@pytest.mark.asyncio
async def test_snapshot_asks_for_stored_line_endings() -> None:
    content, service = await _snapshot(_caps(backup_files.LINE_ENDINGS_CAPABILITY))

    assert content == "a\r\nb\r\n"
    assert service.await_args.args[1:] == (
        "read_file",
        {"path": "www/a.css", "keep_line_endings": True},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caps", [None, _caps("search")], ids=["no-caps", "older"])
async def test_older_component_is_not_sent_the_field(caps: Any) -> None:
    """Its strict read_file schema would refuse the call, and with it the write."""
    _, service = await _snapshot(caps)

    assert service.await_args.args[2] == {"path": "www/a.css"}


def test_file_domains_snapshot_as_stored() -> None:
    registered: dict[str, Any] = {}
    manager = MagicMock()
    manager.register = lambda handler: registered.update({handler.domain: handler})

    backup_manager.register_default_handlers(manager, MagicMock())

    assert registered["file"].fetch is backup_files._fetch_file_as_stored
    assert registered["yaml_file"].fetch is backup_files._fetch_file_as_stored
