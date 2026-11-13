"""Screenshots attached to the dashboard config tools' get and set results."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult
from ha_mcp.client.rest_client import HomeAssistantClient

from ._dashboard_screenshot_common import (
    PNG,
    clear_screenshot_env,
    fake_dashboard_capture,
    set_settings,
)


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


async def test_post_write_render_path_fetch_failure_is_only_a_warning(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("websocket disconnected")

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", boom)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools.attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel"
    )

    assert config_result is None
    assert result["success"] is True
    assert "render_paths" not in result
    assert any("websocket disconnected" in warning for warning in result["warnings"])


async def test_post_write_render_paths_use_authoritative_readback(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    submitted = {"views": [{"title": "Home", "path": "submitted"}]}
    authoritative = {"views": [{"title": "Home", "path": "normalized"}]}

    async def readback(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str]:
        return authoritative, "hash"

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", readback)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools.attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel", submitted
    )

    assert config_result is authoritative
    assert result["render_paths"][0]["render_path"] == "wall-panel/normalized"


async def test_post_write_readback_failure_keeps_only_screenshot_fallback(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    fallback = {"views": [{"title": "Home", "path": "submitted"}]}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("readback failed")

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", boom)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools.attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel", fallback
    )

    assert config_result is fallback
    assert "render_paths" not in result
    assert any("readback failed" in warning for warning in result["warnings"])


async def test_python_transform_can_return_screenshot_from_post_save_config(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    tools = dashboard_tools.DashboardConfigTools(object())
    current = {"views": [{"title": "Old", "path": "home"}]}
    transformed = {"views": [{"title": "New", "path": "home"}]}
    seen: dict[str, Any] = {}

    async def fetch_and_verify(_url_path: str, _config_hash: str) -> dict[str, Any]:
        return current

    def apply_transform(
        _url_path: str, _expression: str, _config: dict[str, Any]
    ) -> dict[str, Any]:
        return transformed

    async def save_transform(
        _url_path: str, _config: dict[str, Any], *, action: str
    ) -> tuple[dict[str, Any], str, str | None]:
        assert action == "python_transform"
        return transformed, "new-hash", None

    async def attach(
        result: dict[str, Any],
        url_path: str,
        requested: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen.update(
            result=result,
            url_path=url_path,
            requested=requested,
            config=kwargs["config"],
        )
        return result

    monkeypatch.setattr(tools, "_fetch_and_verify_dashboard_hash", fetch_and_verify)
    monkeypatch.setattr(tools, "_apply_dashboard_python_transform", apply_transform)
    monkeypatch.setattr(tools, "_save_dashboard_python_transform", save_transform)
    monkeypatch.setattr(dashboard_tools, "maybe_attach_screenshot", attach)

    result = await tools.run_dashboard_python_transform(
        "wall-panel",
        "old-hash",
        "config['views'][0]['title'] = 'New'",
        None,
        False,
        return_screenshot=True,
        screenshot_options=dashboard_tools.DashboardScreenshotOptions(view_path="home"),
    )

    assert result["config_hash"] == "new-hash"
    assert result["render_paths"][0]["render_path"] == "wall-panel/home"
    assert seen == {
        "result": result,
        "url_path": "wall-panel",
        "requested": True,
        "config": transformed,
    }


async def test_python_transform_post_save_read_failure_reports_committed_write(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    tools = dashboard_tools.DashboardConfigTools(object())
    transformed = {"views": [{"title": "New", "path": "home"}]}

    async def fetch_and_verify(_url_path: str, _config_hash: str) -> dict[str, Any]:
        return {"views": [{"title": "Old", "path": "home"}]}

    def apply_transform(
        _url_path: str, _expression: str, _config: dict[str, Any]
    ) -> dict[str, Any]:
        return transformed

    async def save_transform(
        _url_path: str, _config: dict[str, Any], *, action: str
    ) -> tuple[dict[str, Any], str | None, str | None]:
        assert action == "python_transform"
        return transformed, None, "authoritative reload failed"

    monkeypatch.setattr(tools, "_fetch_and_verify_dashboard_hash", fetch_and_verify)
    monkeypatch.setattr(tools, "_apply_dashboard_python_transform", apply_transform)
    monkeypatch.setattr(tools, "_save_dashboard_python_transform", save_transform)

    result = await tools.run_dashboard_python_transform(
        "wall-panel",
        "old-hash",
        "config['views'][0]['title'] = 'New'",
        None,
        False,
        return_screenshot=False,
        screenshot_options=dashboard_tools.DashboardScreenshotOptions(),
    )

    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert "authoritative reload failed" in result["warnings"][0]
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


async def _wall_panel(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """A storage dashboard "wall-panel" with a view "home", saved through Core."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "wall-panel",
            "title": "Wall",
        }
    )
    assert created["success"], created
    saved = await client.send_websocket_message(
        {
            "type": "lovelace/config/save",
            "url_path": "wall-panel",
            "config": {"views": [{"title": "Home", "path": "home"}]},
        }
    )
    assert saved["success"], saved


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
        await _wall_panel(hass, core_client)

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
        await _wall_panel(hass, core_client)
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
        await _wall_panel(hass, core_client)

        result = await dashboard_tools.DashboardConfigTools(
            core_client
        ).ha_config_get_dashboard(
            url_path="wall-panel",
            include_screenshot=True,
            view_path="home",
        )

        assert isinstance(result, ToolResult)
        assert "theme restore failed (unit)" in result.structured_content["warnings"]
