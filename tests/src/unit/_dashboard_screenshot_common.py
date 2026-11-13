"""Constants and helpers shared by the dashboard-screenshot test modules."""

from __future__ import annotations

from typing import Any, Literal

import pytest

from ha_mcp import config

PNG = b"\x89PNG\r\n\x1a\nunit"


JPEG = b"\xff\xd8\xffunit"


WEBP = b"RIFF\x04\x00\x00\x00WEBPunit"


BMP = b"BM12"


_PUPPET_SCHEMA = [
    {"name": "access_token"},
    {"name": "keep_browser_open"},
    {"name": "home_assistant_url"},
]


def _puppet_options(*, keep_browser_open: bool) -> dict[str, Any]:
    return {
        "access_token": "secret",
        "keep_browser_open": keep_browser_open,
        "home_assistant_url": "http://homeassistant:8123",
    }


def _puppet_info(*, state: str, hostname: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "Puppet",
        "state": state,
        "version": "2.4.9",
        "schema": _PUPPET_SCHEMA,
        "options": _puppet_options(keep_browser_open=False),
    }
    if hostname is not None:
        info["hostname"] = hostname
    return info


def clear_screenshot_env(monkeypatch: Any) -> None:
    for var in (
        "HAMCP_ENABLE_DASHBOARD_SCREENSHOT",
        "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
        "SUPERVISOR_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


def fake_dashboard_capture(
    *,
    width: int = 1280,
    height: int | Literal["auto"] = 800,
    preset: Literal["mobile", "tablet", "desktop"] | None = None,
    image_format: Literal["png", "jpeg", "webp", "bmp"] = "png",
    mime_type: str = "image/png",
    data: bytes = b"\x89PNG\r\n\x1a\nfake",
    legacy_full_page_fallback: bool = False,
) -> Any:
    from ha_mcp.dashboard_screenshot.capture import (
        CaptureOptions,
        DashboardImageCapture,
    )

    return DashboardImageCapture(
        data=data,
        width=width,
        height=height,
        preset=preset,
        orientation="landscape",
        image_format=image_format,
        mime_type=mime_type,
        size_bytes=len(data),
        options=CaptureOptions(
            width=width,
            height=height,
            viewport_presets=None,
            orientation=None,
            zoom=1.0,
            wait_ms=2500,
            full_page=height == "auto",
            theme=None,
            dark_mode=False,
            language=None,
            image_format=image_format,
            render_timeout_seconds=60.0,
        ),
        legacy_full_page_fallback=legacy_full_page_fallback,
    )


def fresh_settings() -> config.Settings:
    """The server's settings, built again from the current environment."""
    config.reset_global_settings()
    return config.get_global_settings()


def set_settings(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> None:
    """Set fields on the server's real settings for one test."""
    settings = config.get_global_settings()
    for name, value in fields.items():
        monkeypatch.setattr(settings, name, value)
