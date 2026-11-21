"""The screenshot engine's capture client: URL and parameter building, image return, errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ._dashboard_screenshot_common import (
    BMP,
    JPEG,
    PNG,
    WEBP,
    clear_screenshot_env,
)
from ._screenshot_engine import Engine, Render
from ._screenshot_engine import engine as engine


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    clear_screenshot_env(monkeypatch)


class TestCapture:
    async def test_builds_url_and_returns_png(self, engine: Engine) -> None:
        from ha_mcp.dashboard_screenshot import capture

        png = b"\x89PNG\r\n\x1a\nfake"
        engine.next_response = Render(200, png)

        captures = await capture.capture_dashboard_images(
            "lovelace/0", width=640, height=480, zoom=1.5, wait_ms=2000
        )
        assert captures[0].data == png
        got = engine.gets[-1]
        assert got["path"] == "/lovelace/0"
        assert got["params"]["viewport"] == "640x480"
        assert got["params"]["zoom"] == "1.5"
        assert got["params"]["wait"] == "2000"
        assert got["params"]["format"] == "png"

    async def test_full_page_uses_native_auto_height(self, engine: Engine) -> None:
        """full_page=True uses the content-sized viewport request."""
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, b"\x89PNG\r\n\x1a\nfake")

        await capture.capture_dashboard_images(
            "lovelace/0", width=1024, height=480, full_page=True
        )
        got = engine.gets[-1]
        assert got["params"]["viewport"] == "1024xauto"

    async def test_auto_height_and_full_page_are_true_custom_aliases(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, PNG)

        explicit = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height="auto"
        )
        alias_a = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=480, full_page=True
        )
        alias_b = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=1600, full_page=True
        )

        assert [request["params"]["viewport"] for request in engine.gets] == [
            "700xauto",
            "700xauto",
            "700xauto",
        ]
        assert [(item.width, item.height, item.orientation) for item in explicit] == [
            (700, "auto", None)
        ]
        assert [(item.width, item.height, item.orientation) for item in alias_a] == [
            (700, "auto", None)
        ]
        assert [(item.width, item.height, item.orientation) for item in alias_b] == [
            (700, "auto", None)
        ]

    async def test_custom_auto_height_rejects_orientation(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", height="auto", orientation="landscape"
            )

        assert "custom auto-height viewport" in str(exc_info.value)

    async def test_preset_full_page_keeps_orientation_support(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, PNG)

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile"],
            orientation="landscape",
            full_page=True,
        )

        assert engine.gets[-1]["params"]["viewport"] == "844xauto"
        assert (result[0].width, result[0].height, result[0].orientation) == (
            844,
            "auto",
            "landscape",
        )

    async def test_explicit_auto_does_not_use_legacy_full_page_fallback(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(400, b"", content_type="text/plain")

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0", height="auto")

        assert len(engine.gets) == 1

    async def test_declared_oversize_rejected_before_body_read(
        self, engine: Engine, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        response = Render(200, b"", content_length=5, hold_body=True)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        engine.next_response = response

        async with asyncio.timeout(3):
            with pytest.raises(ToolError) as exc_info:
                await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["limit_kind"] == "image"
        assert len(engine.gets) == 1

    async def test_chunked_oversize_stops_at_limit(
        self, engine: Engine, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        engine.next_response = Render(200, b"", chunks=[b"123", b"45"])

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["received_bytes"] == 5

    async def test_exact_payload_boundary_succeeds(
        self, engine: Engine, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        engine.next_response = Render(
            200, BMP, content_type="image/bmp", content_length=4
        )

        result = await capture.capture_dashboard_images(
            "lovelace/0", image_format="bmp"
        )

        assert result[0].data == BMP

    async def test_batch_limit_identifies_second_capture(
        self, engine: Engine, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        engine.next_response = Render(
            200, BMP, content_type="image/bmp", content_length=4
        )

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0",
                viewport_presets=["mobile", "desktop"],
                image_format="bmp",
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["limit_kind"] == "batch"
        assert error["capture_index"] == 1
        assert error["completed_count"] == 1
        assert error["preset"] == "desktop"

    async def test_partial_batch_retains_success_and_structured_failure(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = [
            Render(200, PNG),
            None,
        ]
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            render_timeout_seconds=1,
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["mobile"]
        assert failures[0]["error"]["code"] == "TIMEOUT_API_REQUEST"
        assert failures[0]["capture_index"] == 1
        assert failures[0]["completed_count"] == 1
        assert failures[0]["preset"] == "desktop"

    async def test_partial_batch_continues_after_first_failure(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = [
            None,
            Render(200, PNG),
        ]
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            render_timeout_seconds=1,
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["desktop"]
        assert failures[0]["capture_index"] == 0
        assert failures[0]["completed_count"] == 0
        assert failures[0]["preset"] == "mobile"

    async def test_all_failed_batch_raises_ordered_aggregate(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = [
            None,
            None,
        ]
        failures: list[dict[str, Any]] = []

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0",
                viewport_presets=["mobile", "desktop"],
                render_timeout_seconds=1,
                partial_failures=failures,
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "TIMEOUT_API_REQUEST"
        assert error["all_captures_failed"] is True
        assert error["failure_count"] == 2
        assert [
            failure["capture_index"] for failure in error["screenshot_failures"]
        ] == [0, 1]
        assert [failure["preset"] for failure in error["screenshot_failures"]] == [
            "mobile",
            "desktop",
        ]

    async def test_batch_cap_returns_prior_capture_as_partial(
        self, engine: Engine, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        engine.next_response = Render(
            200, BMP, content_type="image/bmp", content_length=4
        )
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            image_format="bmp",
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["mobile"]
        assert failures[0]["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert failures[0]["limit_kind"] == "batch"
        assert failures[0]["capture_index"] == 1

    async def test_batch_presets_forward_deterministic_context(
        self, engine: Engine
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, JPEG, content_type="image/jpeg")

        captures = await capture.capture_dashboard_images(
            "wall-panel/home",
            viewport_presets=["mobile", "desktop"],
            orientation="landscape",
            zoom=1.25,
            wait_ms=1234,
            theme="Gerry Dark",
            dark_mode=True,
            language="de",
            image_format="jpeg",
            render_timeout_seconds=90,
        )

        assert [item.preset for item in captures] == ["mobile", "desktop"]
        assert [(item.width, item.height) for item in captures] == [
            (844, 390),
            (1280, 800),
        ]
        assert all(item.mime_type == "image/jpeg" for item in captures)
        assert all(item.options.theme == "Gerry Dark" for item in captures)
        assert [request["params"]["viewport"] for request in engine.gets] == [
            "844x390",
            "1280x800",
        ]
        for request in engine.gets:
            assert request["params"] == {
                "viewport": request["params"]["viewport"],
                "zoom": "1.25",
                "wait": "1234",
                "format": "jpeg",
                "theme": "Gerry Dark",
                "dark": "",
                "lang": "de",
            }

    async def test_rejects_unexpected_content_type(self, engine: Engine) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, b"not a jpeg", content_type="image/png")

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0", image_format="jpeg")
        assert "unexpected content type" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        ("image_format", "mime_type", "body"),
        [
            ("png", "image/png", PNG),
            ("jpeg", "image/jpeg", JPEG),
            ("webp", "image/webp", WEBP),
            ("bmp", "image/bmp", BMP),
        ],
    )
    async def test_accepts_matching_image_signatures(
        self,
        engine: Engine,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
        body: bytes,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, body, content_type=mime_type)

        result = await capture.capture_dashboard_images(
            "lovelace/0", image_format=image_format
        )

        assert result[0].data == body

    @pytest.mark.parametrize(
        ("image_format", "mime_type"),
        [
            ("png", "image/png"),
            ("jpeg", "image/jpeg"),
            ("webp", "image/webp"),
            ("bmp", "image/bmp"),
        ],
    )
    async def test_rejects_mislabeled_non_image_bytes(
        self,
        engine: Engine,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(
            200, b"<html>login</html>", content_type=mime_type
        )

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", image_format=image_format
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error["requested_format"] == image_format
        assert error["received_signature_hex"]

    async def test_timeout_has_distinct_structured_error(self, engine: Engine) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = None

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", render_timeout_seconds=1
            )
        assert "TIMEOUT_API_REQUEST" in str(exc_info.value)

    async def test_rejects_duplicate_viewport_presets(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", viewport_presets=["mobile", "mobile"]
            )
        assert "without duplicate presets" in str(exc_info.value)

    async def test_http_error_raises_toolerror(self, engine: Engine) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(502, b"", text="bad gateway")

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0")

    async def test_empty_body_raises_toolerror(self, engine: Engine) -> None:
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, b"")

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0")

    @pytest.mark.parametrize(
        "path",
        [
            # A root or empty path makes the engine serve its own UI page.
            "/",
            "/.",
            "",
            "  ",
            "//",
            # Each of these could re-point the credentialed engine elsewhere.
            "http://evil/x",  # scheme injection
            "lovelace/0?x=://evil",  # query + scheme
            "lovelace/../admin",  # traversal
            "user@host/x",  # authority
            "lovelace\\0",  # backslash
            "lovelace/0?a=b",  # query string
            "lovelace/0#frag",  # fragment
            "lovelace/\x01ctrl",  # control character
        ],
    )
    async def test_unsafe_dashboard_paths_never_reach_the_engine(
        self, engine: Engine, path: str
    ) -> None:
        """A path that is not a plain dashboard path is refused before the
        engine is asked for anything."""
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, PNG)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images(path)
        assert engine.gets == []

    async def test_path_segments_are_percent_encoded(self, engine: Engine) -> None:
        """A valid segment with an encodable char reaches the engine
        percent-encoded (defense-in-depth), not raw."""
        from ha_mcp.dashboard_screenshot import capture

        engine.next_response = Render(200, b"\x89PNG\r\n\x1a\nfake")

        await capture.capture_dashboard_images("my dash/0")
        assert engine.gets[-1]["path"] == "/my%20dash/0"
