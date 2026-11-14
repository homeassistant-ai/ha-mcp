"""Screenshots attached to the dashboard config tools' get and set results."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._dashboard_screenshot_common import (
    PNG,
    clear_screenshot_env,
    fake_dashboard_capture,
    set_settings,
    wall_panel,
)
from ._held_command import held_command


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    clear_screenshot_env(monkeypatch)


def test_multiple_capture_content_and_metadata_stay_ordered() -> None:
    from ha_mcp.dashboard_screenshot.content import (
        dashboard_image_content,
        dashboard_screenshot_metadata,
    )

    captures = [
        fake_dashboard_capture(width=390, height=844, preset="mobile"),
        fake_dashboard_capture(width=1280, height=800, preset="desktop"),
    ]

    content = dashboard_image_content(captures)
    metadata = dashboard_screenshot_metadata(captures, "wall-panel/home")

    assert [block.mime_type for block in content] == ["image/png", "image/png"]
    assert [item["content_index"] for item in metadata] == [0, 1]
    assert [item["viewport"]["preset"] for item in metadata] == [
        "mobile",
        "desktop",
    ]
    assert all(item["render_path"] == "wall-panel/home" for item in metadata)
    assert all(len(item["image"]["sha256"]) == 64 for item in metadata)
    assert metadata[0]["engine_request"]["viewport"] == "390x844"
    assert metadata[0]["engine_request"]["format"] == "png"
    assert metadata[0]["local_capture_options"] == {
        "full_page": False,
        "render_timeout_seconds": 60.0,
        "legacy_full_page_fallback": False,
    }


def test_native_image_conversion_failure_reports_second_image_context(
    monkeypatch: Any,
) -> None:
    from ha_mcp.dashboard_screenshot import content as screenshot_content

    original_image = screenshot_content.Image
    image_count = 0

    class _FailingImage:
        def to_image_content(self, *, mime_type: str) -> None:
            raise RuntimeError(f"could not encode {mime_type}")

    def image_factory(*args: Any, **kwargs: Any) -> Any:
        nonlocal image_count
        image_index = image_count
        image_count += 1
        if image_index == 1:
            return _FailingImage()
        return original_image(*args, **kwargs)

    monkeypatch.setattr(screenshot_content, "Image", image_factory)
    captures = [
        fake_dashboard_capture(width=390, height=844, preset="mobile"),
        fake_dashboard_capture(width=1280, height=800, preset="desktop"),
    ]

    with pytest.raises(ToolError) as exc_info:
        screenshot_content.dashboard_image_content(captures)

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "IMAGE_SERIALIZATION_FAILED"
    assert error["content_index"] == 1
    assert error["completed_count"] == 1
    assert error["capture_count"] == 2
    assert error["format"] == "png"
    assert error["mime_type"] == "image/png"
    assert error["size_bytes"] == len(PNG)


@pytest.mark.parametrize(
    ("image_format", "mime_type", "data"),
    [
        ("jpeg", "image/jpeg", b"jpeg"),
        ("webp", "image/webp", b"webp"),
        ("bmp", "image/bmp", b"bmp"),
    ],
)
def test_non_png_native_content_preserves_mime(
    image_format: Literal["jpeg", "webp", "bmp"],
    mime_type: str,
    data: bytes,
) -> None:
    from ha_mcp.dashboard_screenshot.content import dashboard_image_content

    content = dashboard_image_content(
        [
            fake_dashboard_capture(
                image_format=image_format,
                mime_type=mime_type,
                data=data,
            )
        ]
    )

    assert content[0].mime_type == mime_type


class TestMaybeAttachScreenshot:
    async def test_skips_when_not_requested(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        result = {"success": True}
        out = await maybe_attach_screenshot(result, "default", requested=False)
        assert out is result

    async def test_feature_off_warns(self, monkeypatch: Any) -> None:
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=False)
        result: dict[str, Any] = {"success": True}
        out = await maybe_attach_screenshot(result, "default", requested=True)
        assert out is result
        assert any("disabled" in w.lower() for w in result.get("warnings", []))

    async def test_capture_failure_warns(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def boom(*_a: Any, **_kw: Any) -> list[Any]:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_images", boom)

        result: dict[str, Any] = {"success": True}
        out = await maybe_attach_screenshot(result, "my-dash", requested=True)
        assert out is result
        assert any("unavailable" in w.lower() for w in result.get("warnings", []))

    async def test_full_page_passed_through(self, monkeypatch: Any) -> None:
        """maybe_attach_screenshot forwards full_page to the capture call."""
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        seen: dict[str, Any] = {}

        async def record(_path: str, **kw: Any) -> list[Any]:
            seen["full_page"] = kw.get("full_page")
            seen["path"] = _path
            return [fake_dashboard_capture(height="auto")]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)

        await maybe_attach_screenshot(
            {"success": True}, "my-dash", requested=True, full_page=True
        )
        assert seen["full_page"] is True
        # A concrete url_path passes through unchanged to the engine path.
        assert seen["path"] == "my-dash"

    async def test_success_returns_toolresult_with_image(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp._vendor.fastmcp.tools import ToolResult
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def ok(*_a: Any, **_kw: Any) -> list[Any]:
            return [fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)

        result: dict[str, Any] = {"success": True}
        out = await maybe_attach_screenshot(result, "my-dash", requested=True)
        # Success returns a ToolResult so structured_content (the dict) is
        # present on both the screenshot and no-screenshot paths, plus the PNG
        # as an image content block.
        assert isinstance(out, ToolResult)
        assert out.structured_content["success"] is True
        assert len(out.content) == 1
        assert out.content[0].type == "image"
        assert out.structured_content["screenshot_render_path"] == "my-dash"
        assert out.structured_content["screenshots"][0]["content_index"] == 0
        assert (
            out.structured_content["screenshots"][0]["frontend_context_confirmed"]
            is False
        )
        assert "screenshots" not in result

    async def test_legacy_fallback_warning_is_preserved_on_config_path(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def ok(*_a: Any, **_kw: Any) -> list[Any]:
            return [fake_dashboard_capture(height=4096, legacy_full_page_fallback=True)]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)

        out = await maybe_attach_screenshot(
            {"success": True}, "my-dash", requested=True, full_page=True
        )

        assert isinstance(out, ToolResult)
        assert any("4096" in warning for warning in out.structured_content["warnings"])
        assert (
            out.structured_content["screenshots"][0]["local_capture_options"][
                "legacy_full_page_fallback"
            ]
            is True
        )

    async def test_named_view_and_batch_options_pass_through(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import (
            DashboardScreenshotOptions,
            maybe_attach_screenshot,
        )

        set_settings(monkeypatch, enable_dashboard_screenshot=True)
        seen: dict[str, Any] = {}

        async def record(path: str, **kwargs: Any) -> list[Any]:
            seen["path"] = path
            seen["kwargs"] = kwargs
            return [
                fake_dashboard_capture(width=390, height=844, preset="mobile"),
                fake_dashboard_capture(width=1280, height=800, preset="desktop"),
            ]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)
        result: dict[str, Any] = {"success": True}
        out = await maybe_attach_screenshot(
            result,
            "wall-panel",
            requested=True,
            config={"views": [{"title": "Home", "path": "home"}]},
            options=DashboardScreenshotOptions(
                view_path="home",
                viewport_presets=["mobile", "desktop"],
                theme="Gerry Dark",
                dark_mode=True,
                language="de",
            ),
        )

        assert seen["path"] == "wall-panel/home"
        assert seen["kwargs"]["viewport_presets"] == ["mobile", "desktop"]
        assert seen["kwargs"]["theme"] == "Gerry Dark"
        assert isinstance(out, ToolResult)
        assert len(out.content) == 2
        assert out.structured_content["screenshot_render_path"] == "wall-panel/home"

    async def test_partial_batch_metadata_is_preserved_on_config_path(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def partial(*_args: Any, **kwargs: Any) -> list[Any]:
            kwargs["partial_failures"].append(
                {
                    "success": False,
                    "error": {"code": "TIMEOUT_API_REQUEST", "message": "timeout"},
                    "capture_index": 1,
                    "preset": "desktop",
                }
            )
            return [fake_dashboard_capture(width=390, height=844, preset="mobile")]

        monkeypatch.setattr(capture, "capture_dashboard_images", partial)

        out = await maybe_attach_screenshot(
            {"success": True}, "wall-panel", requested=True
        )

        assert isinstance(out, ToolResult)
        assert len(out.content) == 1
        assert out.structured_content["screenshot_partial"] is True
        assert out.structured_content["screenshot_failures"][0]["preset"] == ("desktop")

    @pytest.mark.parametrize("raw_error", ["null", "[]"])
    async def test_non_object_tool_error_never_breaks_committed_result(
        self, monkeypatch: Any, raw_error: str
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def fail(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise ToolError(raw_error)

        monkeypatch.setattr(capture, "capture_dashboard_images", fail)
        result: dict[str, Any] = {"success": True, "write_committed": True}

        out = await maybe_attach_screenshot(result, "wall-panel", requested=True)

        assert out is result
        assert out["success"] is True
        assert out["screenshot_error"]["code"] == "INTERNAL_ERROR"

    async def test_serialization_failure_does_not_publish_false_image_metadata(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools import tools_config_dashboards as dashboard_tools

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def ok(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [fake_dashboard_capture()]

        def serialization_failure(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise ToolError(
                json.dumps(
                    {
                        "success": False,
                        "error": {
                            "code": "IMAGE_SERIALIZATION_FAILED",
                            "message": "encode failed",
                        },
                    }
                )
            )

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)
        monkeypatch.setattr(
            dashboard_tools, "dashboard_image_content", serialization_failure
        )
        result: dict[str, Any] = {"success": True, "write_committed": True}

        out = await dashboard_tools.maybe_attach_screenshot(
            result, "wall-panel", requested=True
        )

        assert out is result
        assert out["screenshot_error"]["code"] == "IMAGE_SERIALIZATION_FAILED"
        assert "screenshots" not in out
        assert "screenshot_render_path" not in out

    async def test_base_route_warning_is_preserved_when_config_is_available(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def ok(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)
        result: dict[str, Any] = {"success": True}

        await maybe_attach_screenshot(
            result,
            "wall-panel",
            requested=True,
            config={"views": [{"title": "Home", "path": "home"}]},
        )

        assert any("first view visible" in warning for warning in result["warnings"])

    async def test_get_path_raises_on_capture_failure(self, monkeypatch: Any) -> None:
        """raise_on_failure (the get path) propagates the engine error instead
        of demoting it to a warning the caller may never read."""
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        set_settings(monkeypatch, enable_dashboard_screenshot=True)

        async def boom(*_a: Any, **_kw: Any) -> list[Any]:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_images", boom)

        with pytest.raises(ToolError):
            await maybe_attach_screenshot(
                {"success": True}, "my-dash", requested=True, raise_on_failure=True
            )

    async def test_full_page_without_request_warns(self) -> None:
        """full_page is meaningless without a screenshot flag — make the
        dropped request observable rather than a silent no-op."""
        from ha_mcp.tools.tools_config_dashboards import maybe_attach_screenshot

        result: dict[str, Any] = {"success": True}
        out = await maybe_attach_screenshot(
            result, "my-dash", requested=False, full_page=True
        )
        assert out is result
        assert any(
            "render options are ignored" in w for w in result.get("warnings", [])
        )


@pytest.mark.parametrize(
    "fallback",
    [None, {"views": [{"title": "Home", "path": "submitted"}]}],
    ids=["no-fallback", "submitted-fallback"],
)
async def test_post_write_readback_without_answer_is_only_a_warning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    fallback: dict[str, Any] | None,
) -> None:
    """A landed write stays a success when its readback gets no answer; with
    no readback there are no render paths, only a screenshot fallback."""
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    await wall_panel(hass, core_client)
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("websocket disconnected")),
    )
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools.attach_dashboard_render_paths_after_write(
        core_client, result, "wall-panel", fallback
    )

    assert config_result is fallback
    assert result["success"] is True
    assert "render_paths" not in result
    assert any("websocket disconnected" in warning for warning in result["warnings"])


async def test_post_write_render_paths_come_from_cores_readback(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Render paths name the views Core stored, not the ones submitted."""
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    await wall_panel(hass, core_client)
    submitted = {"views": [{"title": "Home", "path": "submitted"}]}
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools.attach_dashboard_render_paths_after_write(
        core_client, result, "wall-panel", submitted
    )

    assert config_result is not None
    assert config_result["views"][0]["path"] == "home"
    assert result["render_paths"][0]["render_path"] == "wall-panel/home"


async def _transform_hash(client: HomeAssistantClient) -> str:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    read = await dashboard_tools.DashboardConfigTools(client).ha_config_get_dashboard(
        url_path="wall-panel"
    )
    assert isinstance(read, dict), read
    config_hash: str = read["config_hash"]
    return config_hash


async def test_python_transform_screenshot_shows_the_saved_config(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    set_settings(
        monkeypatch, enable_dashboard_screenshot=True, enable_mandatory_bps=False
    )
    await wall_panel(hass, core_client)
    config_hash = await _transform_hash(core_client)
    seen: dict[str, Any] = {}

    async def attach(
        result: dict[str, Any], url_path: str, requested: bool, **kwargs: Any
    ) -> dict[str, Any]:
        seen.update(url_path=url_path, requested=requested, config=kwargs["config"])
        return result

    monkeypatch.setattr(dashboard_tools, "maybe_attach_screenshot", attach)

    result = await dashboard_tools.DashboardConfigTools(
        core_client
    ).ha_config_set_dashboard(
        url_path="wall-panel",
        config_hash=config_hash,
        python_transform="config['views'][0]['title'] = 'New'",
        return_screenshot=True,
        view_path="home",
    )

    stored = await core_client.send_websocket_message(
        {"type": "lovelace/config", "url_path": "wall-panel"}
    )
    assert isinstance(result, dict), result
    assert result["render_paths"][0]["render_path"] == "wall-panel/home"
    assert seen == {
        "url_path": "wall-panel",
        "requested": True,
        "config": stored["result"],
    }
    assert seen["config"]["views"][0]["title"] == "New"


async def test_python_transform_without_post_save_answer_reports_committed_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    set_settings(monkeypatch, enable_mandatory_bps=False)
    await wall_panel(hass, core_client)
    config_hash = await _transform_hash(core_client)
    tools = dashboard_tools.DashboardConfigTools(core_client)

    async with held_command(hass, "lovelace/config/save") as held:
        edit = asyncio.create_task(
            tools.ha_config_set_dashboard(
                url_path="wall-panel",
                config_hash=config_hash,
                python_transform="config['views'][0]['title'] = 'New'",
            )
        )
        await asyncio.wait_for(held.arrived.wait(), 2)
        with monkeypatch.context() as no_answer:
            # The save reached Core; its readback then gets no answer.
            no_answer.setattr(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            )
            held.release.set()
            result = await edit

    stored = await core_client.send_websocket_message(
        {"type": "lovelace/config", "url_path": "wall-panel"}
    )
    assert stored["result"]["views"][0]["title"] == "New"
    assert isinstance(result, dict), result
    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert "could not be reloaded" in result["warnings"][0]
    assert "render_paths" not in result


class TestNoteScreenshotIgnored:
    def test_warns_when_screenshot_requested(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        note_screenshot_ignored(
            result, include_screenshot=True, full_page=False, mode="list"
        )
        assert any("ignored in list mode" in w for w in result["warnings"])

    def test_warns_when_only_full_page_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        note_screenshot_ignored(
            result, include_screenshot=False, full_page=True, mode="search"
        )
        assert any("ignored in search mode" in w for w in result["warnings"])

    def test_silent_when_neither_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        note_screenshot_ignored(
            result, include_screenshot=False, full_page=False, mode="list"
        )
        assert "warnings" not in result

    def test_warns_when_non_default_render_option_is_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import (
            DashboardScreenshotOptions,
            note_screenshot_ignored,
        )

        result: dict[str, Any] = {"success": True}
        note_screenshot_ignored(
            result,
            include_screenshot=False,
            options=DashboardScreenshotOptions(language="de"),
            mode="search",
        )
        assert any("ignored in search mode" in w for w in result["warnings"])


class TestAttachScreenshotToolError:
    def test_error_payload_warnings_merge_into_result_warnings(self) -> None:
        """Theme-guard warnings on a failed capture survive the degraded path."""
        from ha_mcp.tools.tools_config_dashboards import attach_screenshot_tool_error

        error = ToolError(
            json.dumps(
                {
                    "success": False,
                    "error": {"code": "SERVICE_CALL_FAILED", "message": "boom"},
                    "warnings": ["theme restore failed (unit)"],
                }
            )
        )
        result = attach_screenshot_tool_error({"success": True}, error)

        assert "theme restore failed (unit)" in result["warnings"]
        # Kept out of screenshot_error so the warning isn't duplicated.
        assert "warnings" not in result["screenshot_error"]


class TestPublicScreenshotOptionForwarding:
    """Public dashboard get/set methods forward view_path only.

    The advanced render params (width, zoom, theme, etc.) live solely on the
    dedicated ha_get_dashboard_screenshot tool (progressive disclosure) — the
    config tools no longer accept them, so capture always runs with
    screenshot-option defaults besides view_path.
    """

    @staticmethod
    def _assert_capture_call(capture_call: dict[str, Any]) -> None:
        from dataclasses import asdict

        from ha_mcp.tools.tools_config_dashboards import DashboardScreenshotOptions

        assert capture_call["path"] == "wall-panel/home"
        defaults = asdict(DashboardScreenshotOptions())
        del defaults["view_path"]
        assert {
            key: value
            for key, value in capture_call.items()
            if key not in {"path", "partial_failures", "client", "capture_warnings"}
        } == defaults
        assert capture_call["partial_failures"] == []
        # The theme guard needs the HA client for its non-add-on credential
        # fallback and an accumulator for its non-fatal warnings.
        assert capture_call["client"] is not None
        assert capture_call["capture_warnings"] == []

    async def test_get_include_screenshot_forwards_view_path(
        self, monkeypatch: Any, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp._vendor.fastmcp.tools import ToolResult
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools import tools_config_dashboards as dashboard_tools

        set_settings(
            monkeypatch, enable_dashboard_screenshot=True, enable_mandatory_bps=False
        )
        capture_call: dict[str, Any] = {}

        async def record(path: str, **kwargs: Any) -> list[Any]:
            capture_call.update(path=path, **kwargs)
            return [fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)
        await wall_panel(hass, core_client)

        result = await dashboard_tools.DashboardConfigTools(
            core_client
        ).ha_config_get_dashboard(
            url_path="wall-panel",
            include_screenshot=True,
            view_path="home",
        )

        assert isinstance(result, ToolResult)
        self._assert_capture_call(capture_call)

    async def test_set_return_screenshot_forwards_view_path(
        self, monkeypatch: Any, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        from ha_mcp._vendor.fastmcp.tools import ToolResult
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools import tools_config_dashboards as dashboard_tools

        set_settings(
            monkeypatch,
            enable_dashboard_screenshot=True,
            enable_mandatory_bps=False,
            enable_auto_backup=False,
        )
        capture_call: dict[str, Any] = {}

        async def record(path: str, **kwargs: Any) -> list[Any]:
            capture_call.update(path=path, **kwargs)
            return [fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)
        await wall_panel(hass, core_client)
        dashboard_config = {
            "views": [{"title": "Home", "path": "home"}],
        }

        result = await dashboard_tools.DashboardConfigTools(
            core_client
        ).ha_config_set_dashboard(
            url_path="wall-panel",
            config=dashboard_config,
            return_screenshot=True,
            MandatoryBPS=False,
            view_path="home",
        )

        assert isinstance(result, ToolResult)
        self._assert_capture_call(capture_call)

    async def test_include_screenshot_surfaces_theme_guard_warning(
        self, monkeypatch: Any, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A failed theme restore must surface in the config-tool response."""
        from ha_mcp._vendor.fastmcp.tools import ToolResult
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools import tools_config_dashboards as dashboard_tools

        set_settings(
            monkeypatch, enable_dashboard_screenshot=True, enable_mandatory_bps=False
        )

        async def record(path: str, **kwargs: Any) -> list[Any]:
            kwargs["capture_warnings"].append("theme restore failed (unit)")
            return [fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)
        await wall_panel(hass, core_client)

        result = await dashboard_tools.DashboardConfigTools(
            core_client
        ).ha_config_get_dashboard(
            url_path="wall-panel",
            include_screenshot=True,
            view_path="home",
        )

        assert isinstance(result, ToolResult)
        assert "theme restore failed (unit)" in result.structured_content["warnings"]
