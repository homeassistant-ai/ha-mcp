"""Construct the real server with isolated skills and a real Core client."""

from pathlib import Path

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils import skill_loader


def make_server(
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    skills_dir: Path | None = None,
    **settings: object,
) -> HomeAssistantSmartMCPServer:
    current = get_global_settings()
    for key, value in {
        "enabled_tool_modules": "tools_search",
        "enable_tool_search": False,
        "enable_lite_docstrings": False,
        **settings,
    }.items():
        monkeypatch.setattr(current, key, value)
    monkeypatch.setattr(skill_loader, "get_skills_dir", lambda: skills_dir)
    return HomeAssistantSmartMCPServer(client=client)
