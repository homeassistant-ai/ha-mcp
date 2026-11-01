"""A screenshot's theme is checked against the themes Home Assistant has."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot import capture
from ha_mcp.dashboard_screenshot.provision import EngineTarget

from .test_dashboard_screenshot import _PNG, _FakeAsyncClient, _FakeResponse

# Core's frontend/get_themes answer: themes with both modes, no modes, and
# only a dark mode.
_THEMES = {
    "themes": {
        "Midnight": {"modes": {"light": {}, "dark": {}}},
        "Flat Blue": {"primary-color": "#5294e2"},
        "Night Only": {"modes": {"dark": {}}},
    },
    "default_theme": "default",
    "default_dark_theme": None,
}


class _Client:
    def __init__(self, themes_reply: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.themes_reply = themes_reply or {"success": True, "result": _THEMES}

    async def send_websocket_message(self, message: dict[str, Any]) -> Any:
        self.sent.append(message)
        if message["type"] == "frontend/get_themes":
            if isinstance(self.themes_reply, Exception):
                raise self.themes_reply
            return self.themes_reply
        return {"success": False, "error": "not served by this test"}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    async def resolve() -> EngineTarget:
        asked.append("engine")
        return EngineTarget(url="http://engine:10000")

    monkeypatch.setattr(capture, "resolve_engine", resolve)
    _FakeAsyncClient._next = _FakeResponse(200, _PNG)
    monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
    return asked


async def test_a_theme_home_assistant_does_not_have_is_refused_before_rendering(
    engine: list[str],
) -> None:
    """A misspelled theme otherwise renders the default theme with no warning."""
    client = _Client()
    with pytest.raises(ToolError) as exc_info:
        await capture.capture_dashboard_images(
            "lovelace/0", theme="Midnite", client=client
        )

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert {"Midnight", "Flat Blue"} <= set(body["installed_themes"])
    assert client.sent == [{"type": "frontend/get_themes"}]
    assert engine == []


@pytest.mark.parametrize("theme", ["Midnight", "default"])
async def test_installed_and_built_in_themes_render(
    engine: list[str], theme: str
) -> None:
    warnings: list[str] = []
    captures = await capture.capture_dashboard_images(
        "lovelace/0",
        theme=theme,
        dark_mode=True,
        client=_Client(),
        capture_warnings=warnings,
    )

    assert captures[0].data == _PNG
    assert not any(theme in w for w in warnings)


@pytest.mark.parametrize(
    ("theme", "dark_mode", "rendered"),
    [("Flat Blue", True, "no dark variant"), ("Night Only", False, "no light variant")],
    ids=["no-dark-mode", "dark-only"],
)
async def test_a_mode_the_theme_does_not_define_is_reported(
    engine: list[str], theme: str, dark_mode: bool, rendered: str
) -> None:
    """dark_mode naming a mode the theme does not define is reported."""
    warnings: list[str] = []
    await capture.capture_dashboard_images(
        "lovelace/0",
        theme=theme,
        dark_mode=dark_mode,
        client=_Client(),
        capture_warnings=warnings,
    )

    assert any(theme in w and rendered in w for w in warnings)


@pytest.mark.parametrize(
    "reply",
    [
        ConnectionError("websocket closed"),
        {"success": False, "error": "Unauthorized"},
    ],
    ids=["transport-failure", "core-refusal"],
)
async def test_an_unreadable_theme_list_still_renders_and_says_why(
    engine: list[str], reply: Any
) -> None:
    """The theme pre-check is optional; its failure must not fail the render."""
    warnings: list[str] = []
    captures = await capture.capture_dashboard_images(
        "lovelace/0",
        theme="Midnight",
        client=_Client(reply),
        capture_warnings=warnings,
    )

    assert captures[0].data == _PNG
    reason = "websocket closed" if isinstance(reply, Exception) else "Unauthorized"
    assert any("not checked" in w and reason in w for w in warnings)
