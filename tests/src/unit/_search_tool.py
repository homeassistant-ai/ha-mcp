"""Build ``ha_search`` on a client, for the search routing tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


def _build_ha_search(client: Any) -> Any:
    """``ha_search`` registered on ``client``: the real client, or an older
    test's own client until that test is converted."""
    mcp = MagicMock()
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp.add_tool = capture_add_tool
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_search"]


def _setup_visibility_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
