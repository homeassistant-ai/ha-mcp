"""The themes section of ``ha_get_system_health``.

The tool runs on the server's own client against a real ``hass``: Core's
``system_health`` answers the baseline and Core's ``frontend`` answers
``frontend/get_themes``, so every theme listing and refusal is Core's own.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandTimeout
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import tools_system
from ha_mcp.tools.tools_system import SystemTools

_THEMES = {
    "default": {},
    "dark_blue": {"primary-color": "#1a237e"},
    "light_green": {"primary-color": "#2e7d32"},
}


@pytest.fixture
async def system_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> SystemTools:
    assert await async_setup_component(hass, "system_health", {})
    return SystemTools(core_client)


async def _set_up_frontend(hass: HomeAssistant, tmp_path: Path) -> None:
    """Core's frontend with ``_THEMES``. The development-repo path keeps it off
    the frontend package, whose files no test here asks for."""
    config = {"frontend": {"development_repo": str(tmp_path), "themes": _THEMES}}
    assert await async_setup_component(hass, "frontend", config)


def _on_each_socket(
    monkeypatch: pytest.MonkeyPatch,
    stack: ExitStack,
    patch_socket: Callable[[HomeAssistantWebSocketClient], AbstractContextManager[Any]],
) -> list[Any]:
    """Apply ``patch_socket`` to every WebSocket the tool opens, once connected.

    Returns what each patch yields; the patches end with ``stack``.
    """
    real_connect = tools_system.get_connected_ws_client
    entered: list[Any] = []

    async def connect(
        *args: Any, **kwargs: Any
    ) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
        ws, error = await real_connect(*args, **kwargs)
        if ws is not None:
            entered.append(stack.enter_context(patch_socket(ws)))
        return ws, error

    monkeypatch.setattr(tools_system, "get_connected_ws_client", connect)
    return entered


class TestFetchThemes:
    """The themes section reports Core's listing, or why it has none."""

    async def test_happy_path_with_themes(
        self, hass: HomeAssistant, system_tools: SystemTools, tmp_path: Path
    ) -> None:
        """The section returns sorted theme names plus defaults."""
        await _set_up_frontend(hass, tmp_path)
        await hass.services.async_call(
            "frontend",
            "set_theme",
            {"name": "dark_blue", "mode": "dark"},
            blocking=True,
        )

        result = (await system_tools.ha_get_system_health(include="themes"))["themes"]

        assert result["count"] == 3
        assert result["themes"] == ["dark_blue", "default", "light_green"]
        assert result["default_theme"] == "default"
        assert result["default_dark_theme"] == "dark_blue"

    async def test_recovery_mode_shape_missing_default_dark_theme(
        self, hass: HomeAssistant, system_tools: SystemTools, tmp_path: Path
    ) -> None:
        """In recovery mode, Core returns no themes and no default_dark_theme key."""
        await _set_up_frontend(hass, tmp_path)
        hass.config.recovery_mode = True

        result = (await system_tools.ha_get_system_health(include="themes"))["themes"]

        assert result["count"] == 0
        assert result["themes"] == []
        assert result["default_theme"] == "default"
        assert result["default_dark_theme"] is None

    async def test_ws_failure_path(self, system_tools: SystemTools) -> None:
        """Core refusing frontend/get_themes (no frontend loaded) surfaces an error."""
        result = (await system_tools.ha_get_system_health(include="themes"))["themes"]

        assert result["count"] == 0
        assert result["themes"] == []
        assert "Unknown command" in result["error"]

    async def test_ws_exception_captured_as_error(
        self,
        hass: HomeAssistant,
        system_tools: SystemTools,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No answer to the themes command is caught and surfaced via error."""
        await _set_up_frontend(hass, tmp_path)
        with ExitStack() as stack:
            _on_each_socket(
                monkeypatch,
                stack,
                lambda ws: patch.object(
                    ws,
                    "send_command",
                    AsyncMock(
                        side_effect=HomeAssistantCommandTimeout("Command timeout")
                    ),
                ),
            )
            health = await system_tools.ha_get_system_health(include="themes")

        assert health["themes"]["count"] == 0
        assert "Command timeout" in health["themes"]["error"]

    async def test_valid_include_accepted(
        self,
        hass: HomeAssistant,
        system_tools: SystemTools,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """include='themes' is a known section: no warning, and it is fetched once."""
        await _set_up_frontend(hass, tmp_path)
        with ExitStack() as stack:
            spies = _on_each_socket(
                monkeypatch,
                stack,
                lambda ws: patch.object(ws, "send_command", wraps=ws.send_command),
            )
            result = await system_tools.ha_get_system_health(include="themes")

        assert "themes" in result
        assert result.get("warnings") is None or not any(
            "Unknown include" in w for w in result.get("warnings", [])
        )
        theme_fetches = [
            call
            for spy in spies
            for call in spy.call_args_list
            if call.args[0] == "frontend/get_themes"
        ]
        assert len(theme_fetches) == 1
