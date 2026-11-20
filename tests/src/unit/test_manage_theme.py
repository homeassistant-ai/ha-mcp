"""``ha_manage_theme`` list and set, against Core's own ``frontend``.

The tool runs on the server's own client connected to a real ``hass``: Core's
``frontend`` answers ``frontend/get_themes`` and validates ``frontend.set_theme``,
so every listing, default and refusal is Core's own.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandNotSent
from ha_mcp.tools.helpers import core_error_message
from ha_mcp.tools.tools_themes import ThemesTools

from ._command_faults import received, unanswered
from ._tool_errors import rejected_call_body

_THEMES = {
    "nord": {
        "primary-color": "#5e81ac",
        "modes": {"dark": {"primary-color": "#88c0d0"}},
    },
    "iceberg": {},
}


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


async def _cores_refusal(
    client: HomeAssistantClient, service_data: dict[str, Any]
) -> str:
    """Core's own text when it refuses ``frontend.set_theme`` with ``service_data``."""
    reply = await client.send_websocket_message(
        {
            "type": "call_service",
            "domain": "frontend",
            "service": "set_theme",
            "service_data": service_data,
        }
    )
    assert not reply["success"], reply
    return core_error_message(reply["error"])


class TestManageThemeList:
    """action='list' reports Core's installed themes and defaults."""

    async def test_list_names_every_theme_and_both_defaults(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend",
            "set_theme",
            {"name": "nord", "name_dark": "iceberg"},
            blocking=True,
        )

        result = await ThemesTools(core_client).ha_manage_theme(action="list")

        assert result["success"] is True
        data = result["data"]
        assert data["themes"] == ["iceberg", "nord"]
        assert data["count"] == 2
        assert data["default_theme"] == "nord"
        assert data["default_dark_theme"] == "iceberg"

    async def test_list_shows_which_modes_a_theme_declares(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """Without the modes a caller cannot tell which theme has a dark variant."""
        await _set_up_frontend(
            hass,
            tmp_path,
            themes={
                **_THEMES,
                "both": {"modes": {"light": {"a": "b"}, "dark": {"a": "c"}}},
            },
        )

        result = await ThemesTools(core_client).ha_manage_theme(action="list")

        assert result["data"]["modes"] == {
            "both": ["dark", "light"],
            "nord": ["dark"],
        }

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
        payload = await rejected_call_body(
            ThemesTools(core_client).ha_manage_theme(action="list")
        )
        assert payload["success"] is False
        assert "Failed to list themes" in payload["error"]["message"]
        assert "Unknown command" in payload["error"]["message"]


class TestManageThemeSet:
    """action='set' selects Core's backend default themes and reports them."""

    async def test_set_light_default_reports_the_new_defaults(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name="nord"
        )

        assert result["success"] is True
        assert result["data"] == {"default_theme": "nord", "default_dark_theme": None}

    async def test_set_light_and_dark_defaults_in_one_call(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name="iceberg", theme_name_dark="nord"
        )

        assert result["data"] == {
            "default_theme": "iceberg",
            "default_dark_theme": "nord",
        }

    async def test_set_dark_default_alone_leaves_the_light_default(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend", "set_theme", {"name": "iceberg"}, blocking=True
        )

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name_dark="nord"
        )

        assert result["data"] == {
            "default_theme": "iceberg",
            "default_dark_theme": "nord",
        }

    async def test_set_reports_the_default_core_stored_not_the_name_sent(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """Core stores 'none' as no dark override; the result is what Core holds."""
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend", "set_theme", {"name_dark": "nord"}, blocking=True
        )

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set", theme_name_dark="none"
        )

        assert result["data"]["default_dark_theme"] is None

    async def test_set_unknown_theme_keeps_cores_refusal(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """Core names the unknown theme and the field; the tool keeps its text."""
        await _set_up_frontend(hass, tmp_path)

        body = await rejected_call_body(
            ThemesTools(core_client).ha_manage_theme(
                action="set", theme_name_dark="nope"
            )
        )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        cores_text = await _cores_refusal(core_client, {"name_dark": "nope"})
        assert cores_text in body["error"]["message"]
        assert body["ha_error_code"] == "invalid_format"

    async def test_set_without_a_theme_is_left_to_core(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
    ) -> None:
        """Which theme fields a call needs is Core's rule, so Core is asked."""
        await _set_up_frontend(hass, tmp_path)

        with received(hass, "call_service") as seen:
            body = await rejected_call_body(
                ThemesTools(core_client).ha_manage_theme(action="set")
            )

        assert len(seen["call_service"]) == 1
        assert await _cores_refusal(core_client, {}) in body["error"]["message"]

    async def test_set_by_a_non_admin_says_an_admin_token_is_needed(
        self,
        hass: HomeAssistant,
        read_only_client: HomeAssistantClient,
        tmp_path: Path,
    ) -> None:
        """Core refuses a non-admin with a generic code; without the translation
        the caller would be sent looking for a missing theme."""
        await _set_up_frontend(hass, tmp_path)

        body = await rejected_call_body(
            ThemesTools(read_only_client).ha_manage_theme(
                action="set", theme_name="nord"
            )
        )

        assert body["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"

    async def test_set_without_answer_says_the_outcome_is_unknown(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core may have applied the theme, so the caller is told to check."""
        await _set_up_frontend(hass, tmp_path)

        async with unanswered(hass, "call_service", monkeypatch):
            body = await rejected_call_body(
                ThemesTools(core_client).ha_manage_theme(
                    action="set", theme_name="nord"
                )
            )

        assert body["write_outcome_unknown"] is True
        assert "action='list'" in body["error"]["suggestion"]

    async def test_set_never_sent_is_not_reported_as_unknown(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing reached Core, so the caller must not be told to check it."""
        await _set_up_frontend(hass, tmp_path)
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantCommandNotSent("no connection")),
        )

        body = await rejected_call_body(
            ThemesTools(core_client).ha_manage_theme(action="set", theme_name="nord")
        )

        assert "write_outcome_unknown" not in body

    async def test_set_with_unanswered_read_back_is_not_reported_as_failed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core applied the theme; only re-reading the defaults went unanswered."""
        await _set_up_frontend(hass, tmp_path)

        async with unanswered(hass, "frontend/get_themes", monkeypatch):
            body = await rejected_call_body(
                ThemesTools(core_client).ha_manage_theme(
                    action="set", theme_name="nord"
                )
            )

        assert "frontend.set_theme was applied" in body["error"]["message"]
        assert (await _core_defaults(core_client))["default_theme"] == "nord"
