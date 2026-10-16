"""The dashboard snapshot resolves the tools' ``default`` alias to Core's ``lovelace`` item."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.backup_manager as bm
import ha_mcp.tools.tools_config_dashboards as dash


class _StubClient:
    pass


async def test_fetch_dashboard_maps_default_alias_to_lovelace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``url_path='default'`` must capture the ``lovelace`` config, not skip the snapshot."""
    seen: dict[str, Any] = {}
    row = {"id": "lovelace", "url_path": "lovelace", "title": "Home", "mode": "storage"}

    async def fake_resolve(_client: Any, identifier: str) -> Any:
        seen["resolved"] = identifier
        return {"url_path": "lovelace", "id": "lovelace"}, [row]

    async def fake_component(_client: Any, _url_path: str) -> Any:
        return None

    async def fake_get_internal(_client: Any, url_path: str | None) -> Any:
        seen["url_path"] = url_path
        return {"views": [{"title": "Overview"}]}, "hash"

    monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
    monkeypatch.setattr(dash, "_component_dashboard_config", fake_component)
    monkeypatch.setattr(dash, "_get_dashboard_config_internal", fake_get_internal)

    got = await bm.fetch_dashboard(_StubClient(), "default")

    assert got == {"dashboard": row, "config": {"views": [{"title": "Overview"}]}}
    assert seen == {"resolved": "lovelace", "url_path": "lovelace"}


async def test_legacy_read_of_lovelace_leaves_core_to_pick_the_store() -> None:
    """Without the item (deleted, never migrated) Core reads its default store."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock(return_value={"result": {"views": []}})

    await dash._get_dashboard_config_internal(client, "lovelace")

    assert client.send_websocket_message.call_args[0][0] == {
        "type": "lovelace/config",
        "force": True,
    }
