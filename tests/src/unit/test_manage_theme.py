"""``ha_manage_theme`` list and set, against Core's own ``frontend``.

The tool runs on the server's own client connected to a real ``hass``: Core's
``frontend`` answers ``frontend/get_themes`` and validates ``frontend.set_theme``,
so every listing, default and refusal is Core's own.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_themes import ThemesTools

from ._tool_errors import error_body

_THEMES = {"nord": {"primary-color": "#5e81ac"}, "iceberg": {}}


async def _set_up_frontend(
    hass: HomeAssistant, tmp_path: Path, themes: dict[str, Any] | None = None
) -> None:
    """Core's frontend with ``themes``; the development repo keeps it off the
    frontend package, whose files no test here asks for."""
    config = {
        "frontend": {
            "development_repo": str(tmp_path),
            "themes": _THEMES if themes is None else themes,
        }
    }
    assert await async_setup_component(hass, "frontend", config)


async def _core_defaults(client: HomeAssistantClient) -> dict[str, Any]:
    reply = await client.send_websocket_message({"type": "frontend/get_themes"})
    assert reply["success"], reply
    result: dict[str, Any] = reply["result"]
    return result


class TestManageThemeList:
    """action='list' reports Core's installed themes and defaults."""

    async def test_list_returns_sorted_names_and_defaults(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend", "set_theme", {"name": "nord"}, blocking=True
        )
        await hass.services.async_call(
            "frontend", "set_theme", {"name": "iceberg", "mode": "dark"}, blocking=True
        )

        result = await ThemesTools(core_client).ha_manage_theme(action="list")

        assert result["success"] is True
        data = result["data"]
        assert data["themes"] == ["iceberg", "nord"]
        assert data["count"] == 2
        assert data["default_theme"] == "nord"
        assert data["default_dark_theme"] == "iceberg"

    async def test_list_with_no_themes_installed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path, themes={})

        result = await ThemesTools(core_client).ha_manage_theme(action="list")

        assert result["success"] is True
        assert result["data"]["themes"] == []
        assert result["data"]["count"] == 0

    async def test_list_refused_by_core_raises_tool_error(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without the frontend Core refuses frontend/get_themes; the tool says so."""
        with pytest.raises(ToolError) as exc_info:
            await ThemesTools(core_client).ha_manage_theme(action="list")

        payload = error_body(exc_info)
        assert payload["success"] is False
        assert "Failed to list themes" in payload["error"]["message"]
        assert "Unknown command" in payload["error"]["message"]


class TestManageThemeSet:
    """action='set' selects Core's backend default theme and reports it."""

    async def test_set_requires_theme_name(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        with (
            patch.object(
                core_client, "call_service", wraps=core_client.call_service
            ) as call_spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await ThemesTools(core_client).ha_manage_theme(action="set")

        payload = error_body(exc_info)
        assert payload["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
        call_spy.assert_not_awaited()

    async def test_set_light_default_reports_the_new_default(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name="nord"
        )

        assert result["success"] is True
        assert result["data"]["theme"] == "nord"
        assert result["data"]["mode"] == "light"
        assert result["data"]["default_theme"] == "nord"
        assert (await _core_defaults(core_client))["default_theme"] == "nord"

    async def test_set_with_dark_mode_sets_only_the_dark_default(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name="nord", mode="dark"
        )

        assert result["data"]["mode"] == "dark"
        assert result["data"]["default_dark_theme"] == "nord"
        assert result["data"]["default_theme"] == "default"
        core = await _core_defaults(core_client)
        assert core["default_dark_theme"] == "nord"
        assert core["default_theme"] == "default"

    async def test_set_light_mode_leaves_dark_default_untouched(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend", "set_theme", {"name": "iceberg", "mode": "dark"}, blocking=True
        )

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name="nord"
        )

        assert result["data"]["mode"] == "light"
        assert result["data"]["default_theme"] == "nord"
        assert result["data"]["default_dark_theme"] == "iceberg"

    async def test_set_verification_read_without_answer_raises_tool_error(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """The theme is applied, but no answer to the re-read is an error."""
        await _set_up_frontend(hass, tmp_path)
        with (
            patch.object(
                core_client, "call_service", wraps=core_client.call_service
            ) as call_spy,
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("ws dropped")),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await ThemesTools(core_client).ha_manage_theme(
                action="set", theme_name="nord"
            )

        call_spy.assert_awaited_once()
        payload = error_body(exc_info)
        assert payload["success"] is False
        assert "ws dropped" in payload["error"]["message"]
        assert (await _core_defaults(core_client))["default_theme"] == "nord"

    async def test_set_unknown_theme_rejection_names_theme(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """Core's bare 400 for an unknown theme is enriched with the theme name."""
        await _set_up_frontend(hass, tmp_path)

        with pytest.raises(ToolError) as exc_info:
            await ThemesTools(core_client).ha_manage_theme(
                action="set", theme_name="nope"
            )

        payload = error_body(exc_info)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "nope" in payload["error"]["message"]
        assert (await _core_defaults(core_client))["default_theme"] == "default"

    async def test_set_without_answer_raises_tool_error_pointing_to_list(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        with (
            patch.object(
                core_client,
                "call_service",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await ThemesTools(core_client).ha_manage_theme(
                action="set", theme_name="nope"
            )

        payload = error_body(exc_info)
        assert payload["success"] is False
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        assert any(
            "action='list'" in s for s in payload["error"].get("suggestions", [])
        )
