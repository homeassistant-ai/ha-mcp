"""The standalone ha_get_dashboard_screenshot tool."""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.exceptions import ValidationError as FastMCPValidationError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._dashboard_screenshot_common import (
    clear_screenshot_env,
    fake_dashboard_capture,
    set_settings,
    wall_panel,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    clear_screenshot_env(monkeypatch)


@pytest.fixture(autouse=True)
async def _dashboard(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """The "wall-panel" dashboard the tests capture, stored in Core."""
    await wall_panel(hass, core_client)


class TestStandaloneScreenshotTool:
    async def test_viewport_presets_registered_validation_and_schema(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp._vendor.fastmcp import FastMCP
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        captures: list[dict[str, Any]] = []

        async def capture(*_a: Any, **kwargs: Any) -> list[Any]:
            captures.append(kwargs)
            return [fake_dashboard_capture()]

        set_settings(monkeypatch, enable_dashboard_screenshot=True)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        mcp = FastMCP("test")
        mod.register_dashboard_screenshot_tools(mcp, core_client)
        tool = await mcp.get_tool("ha_get_dashboard_screenshot")

        await tool.run(
            {
                "url_path": "wall-panel",
                "width": 2560,
                "viewport_presets": None,
            }
        )
        assert captures[-1]["viewport_presets"] is None
        assert captures[-1]["width"] == 2560

        await tool.run(
            {
                "url_path": "wall-panel",
                "viewport_presets": '["mobile"]',
            }
        )
        assert captures[-1]["viewport_presets"] == ["mobile"]

        for invalid in ([], ["mobile"] * 4):
            with pytest.raises(FastMCPValidationError):
                await tool.run({"viewport_presets": invalid})

        schema = tool.parameters["properties"]["viewport_presets"]
        assert schema["description"].startswith("Render one or more named")
        assert schema["anyOf"][0]["minItems"] == 1
        assert schema["anyOf"][0]["maxItems"] == 3

    async def test_structured_target_returns_ordered_images_and_metadata(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp._vendor.fastmcp.tools import ToolResult
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            return [
                fake_dashboard_capture(width=390, height=844, preset="mobile"),
                fake_dashboard_capture(width=1280, height=800, preset="desktop"),
            ]

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            core_client
        ).ha_get_dashboard_screenshot(
            url_path="wall-panel",
            view_path="home",
            viewport_presets=["mobile", "desktop"],
        )

        assert isinstance(result, ToolResult)
        assert len(result.content) == 2
        assert result.structured_content["render_path"] == "wall-panel/home"
        assert result.structured_content["screenshot_count"] == 2
        assert [
            item["content_index"] for item in result.structured_content["screenshots"]
        ] == [0, 1]

    async def test_theme_guard_warning_reaches_tool_response(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        """A failed theme restore must surface in the standalone tool output."""
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **kwargs: Any) -> list[Any]:
            kwargs["capture_warnings"].append("theme restore failed (unit)")
            return [fake_dashboard_capture()]

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            core_client
        ).ha_get_dashboard_screenshot(url_path="wall-panel", view_path="home")

        assert "theme restore failed (unit)" in result.structured_content["warnings"]

    async def test_packaging_failure_keeps_theme_guard_warning(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        """A restore warning survives even when image packaging fails."""
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **kwargs: Any) -> list[Any]:
            kwargs["capture_warnings"].append("theme restore failed (unit)")
            return [fake_dashboard_capture()]

        def broken_content(*_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("serialization boom")

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)
        monkeypatch.setattr(mod, "dashboard_image_content", broken_content)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(core_client).ha_get_dashboard_screenshot(
                url_path="wall-panel", view_path="home"
            )

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "IMAGE_SERIALIZATION_FAILED"
        assert "theme restore failed (unit)" in payload["warnings"]

    async def test_legacy_full_page_fallback_surfaces_warning(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            return [fake_dashboard_capture(height=4096, legacy_full_page_fallback=True)]

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            core_client
        ).ha_get_dashboard_screenshot(
            url_path="wall-panel", view_path="home", full_page=True
        )

        assert any(
            "4096" in warning for warning in result.structured_content["warnings"]
        )
        assert (
            result.structured_content["screenshots"][0]["local_capture_options"][
                "legacy_full_page_fallback"
            ]
            is True
        )

    async def test_partial_batch_returns_image_and_failure_metadata(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **kwargs: Any) -> list[Any]:
            kwargs["partial_failures"].append(
                {
                    "success": False,
                    "error": {"code": "TIMEOUT_API_REQUEST", "message": "timeout"},
                    "capture_index": 1,
                    "preset": "desktop",
                }
            )
            return [fake_dashboard_capture(width=390, height=844, preset="mobile")]

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            core_client
        ).ha_get_dashboard_screenshot(
            url_path="wall-panel",
            view_path="home",
            viewport_presets=["mobile", "desktop"],
        )

        assert len(result.content) == 1
        assert result.structured_content["partial"] is True
        assert result.structured_content["screenshot_failures"][0]["preset"] == (
            "desktop"
        )

    async def test_raw_capture_failure_without_management_is_structured(
        self, monkeypatch: Any, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("unexpected capture failure")

        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(core_client).ha_get_dashboard_screenshot(
                url_path="wall-panel",
                view_path="home",
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "INTERNAL_ERROR"
        assert error["error"]["details"] == "unexpected capture failure"
        assert error["dashboard_path"] == "wall-panel/home"
