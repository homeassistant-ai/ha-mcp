"""The dashboard snapshot resolves the tools' ``default`` alias to Core's ``lovelace`` item."""

from __future__ import annotations

from typing import Any

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

    async def fake_resolve(_client: Any, identifier: str) -> Any:
        seen["resolved"] = identifier
        return {"url_path": "lovelace", "id": "lovelace"}, None

    async def fake_component(_client: Any, _url_path: str) -> Any:
        return None

    async def fake_get_internal(_client: Any, url_path: str | None) -> Any:
        seen["url_path"] = url_path
        return {"views": [{"title": "Overview"}]}, "hash"

    monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
    monkeypatch.setattr(dash, "_component_dashboard_config", fake_component)
    monkeypatch.setattr(dash, "_get_dashboard_config_internal", fake_get_internal)

    got = await bm._fetch_dashboard(_StubClient(), "default")

    assert got == {"views": [{"title": "Overview"}]}
    assert seen == {"resolved": "lovelace", "url_path": "lovelace"}
