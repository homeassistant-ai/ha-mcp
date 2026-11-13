"""The screenshot engine's capture client: URL and parameter building, image return, errors."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar, Literal

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot.provision import EngineTarget

from ._dashboard_screenshot_common import (
    _BMP,
    _JPEG,
    _PNG,
    _WEBP,
    clear_screenshot_env,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    clear_screenshot_env(monkeypatch)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        content: bytes,
        text: str = "",
        content_type: str = "image/png",
        content_length: int | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = chunks
        self.iterated = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        self.iterated = True
        chunks = self._chunks if self._chunks is not None else [self.content]
        for chunk in chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakeAsyncClient:
    """Minimal async-context httpx.AsyncClient stand-in."""

    last_get: ClassVar[dict[str, Any]] = {}
    gets: ClassVar[list[dict[str, Any]]] = []
    _next: ClassVar[_FakeResponse | Exception | list[_FakeResponse | Exception]]

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def stream(
        self, method: str, url: str, params: dict[str, Any] | None = None
    ) -> _FakeStreamContext:
        _FakeAsyncClient.last_get = {
            "url": url,
            "params": dict(params) if params is not None else None,
        }
        _FakeAsyncClient.gets.append(_FakeAsyncClient.last_get)
        next_response = _FakeAsyncClient._next
        if isinstance(next_response, list):
            next_response = next_response.pop(0)
        return _FakeStreamContext(next_response)


class TestCapture:
    async def test_builds_url_and_returns_png(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        png = b"\x89PNG\r\n\x1a\nfake"
        _FakeAsyncClient._next = _FakeResponse(200, png)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        captures = await capture.capture_dashboard_images(
            "lovelace/0", width=640, height=480, zoom=1.5, wait_ms=2000
        )
        assert captures[0].data == png
        got = _FakeAsyncClient.last_get
        assert got["url"] == "http://engine:10000/lovelace/0"
        assert got["params"]["viewport"] == "640x480"
        assert got["params"]["zoom"] == "1.5"
        assert got["params"]["wait"] == "2000"
        assert got["params"]["format"] == "png"

    async def test_full_page_uses_native_auto_height(self, monkeypatch: Any) -> None:
        """full_page=True uses the content-sized viewport request."""
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        await capture.capture_dashboard_images(
            "lovelace/0", width=1024, height=480, full_page=True
        )
        got = _FakeAsyncClient.last_get
        assert got["params"]["viewport"] == "1024xauto"

    async def test_auto_height_and_full_page_are_true_custom_aliases(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(200, _PNG)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        explicit = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height="auto"
        )
        alias_a = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=480, full_page=True
        )
        alias_b = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=1600, full_page=True
        )

        assert [request["params"]["viewport"] for request in _FakeAsyncClient.gets] == [
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
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, _PNG)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile"],
            orientation="landscape",
            full_page=True,
        )

        assert _FakeAsyncClient.last_get["params"]["viewport"] == "844xauto"
        assert (result[0].width, result[0].height, result[0].orientation) == (
            844,
            "auto",
            "landscape",
        )

    async def test_explicit_auto_does_not_use_legacy_full_page_fallback(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(400, b"", content_type="text/plain")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0", height="auto")

        assert len(_FakeAsyncClient.gets) == 1

    async def test_declared_oversize_rejected_before_body_read(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        response = _FakeResponse(200, b"12345", content_length=5)
        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = response
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["limit_kind"] == "image"
        assert response.iterated is False

    async def test_chunked_oversize_stops_at_limit(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = _FakeResponse(200, b"", chunks=[b"123", b"45"])
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["received_bytes"] == 5

    async def test_exact_payload_boundary_succeeds(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0", image_format="bmp"
        )

        assert result[0].data == _BMP

    async def test_batch_limit_identifies_second_capture(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

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
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = [
            _FakeResponse(200, _PNG),
            httpx.ReadTimeout("desktop timed out"),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["mobile"]
        assert failures[0]["error"]["code"] == "TIMEOUT_API_REQUEST"
        assert failures[0]["capture_index"] == 1
        assert failures[0]["completed_count"] == 1
        assert failures[0]["preset"] == "desktop"

    async def test_partial_batch_continues_after_first_failure(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = [
            httpx.ReadTimeout("mobile timed out"),
            _FakeResponse(200, _PNG),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["desktop"]
        assert failures[0]["capture_index"] == 0
        assert failures[0]["completed_count"] == 0
        assert failures[0]["preset"] == "mobile"

    async def test_all_failed_batch_raises_ordered_aggregate(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = [
            httpx.ReadTimeout("mobile timed out"),
            httpx.ReadTimeout("desktop timed out"),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0",
                viewport_presets=["mobile", "desktop"],
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
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
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
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(200, _JPEG, content_type="image/jpeg")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

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
        assert [request["params"]["viewport"] for request in _FakeAsyncClient.gets] == [
            "844x390",
            "1280x800",
        ]
        for request in _FakeAsyncClient.gets:
            assert request["params"] == {
                "viewport": request["params"]["viewport"],
                "zoom": "1.25",
                "wait": "1234",
                "format": "jpeg",
                "theme": "Gerry Dark",
                "dark": "",
                "lang": "de",
            }

    async def test_rejects_unexpected_content_type(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(
            200, b"not a jpeg", content_type="image/png"
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0", image_format="jpeg")
        assert "unexpected content type" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        ("image_format", "mime_type", "body"),
        [
            ("png", "image/png", _PNG),
            ("jpeg", "image/jpeg", _JPEG),
            ("webp", "image/webp", _WEBP),
            ("bmp", "image/bmp", _BMP),
        ],
    )
    async def test_accepts_matching_image_signatures(
        self,
        monkeypatch: Any,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
        body: bytes,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, body, content_type=mime_type)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

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
        monkeypatch: Any,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(
            200, b"<html>login</html>", content_type=mime_type
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", image_format=image_format
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error["requested_format"] == image_format
        assert error["received_signature_hex"]

    async def test_timeout_has_distinct_structured_error(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = httpx.ReadTimeout("render too slow")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

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

    async def test_http_error_raises_toolerror(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(502, b"", text="bad gateway")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0")

    async def test_empty_body_raises_toolerror(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0")

    @pytest.mark.parametrize("path", ["/", "/.", "", "  ", "//"])
    async def test_root_or_empty_path_rejected(self, path: str) -> None:
        """A root/empty path would make the engine serve its config/UI HTML
        instead of a dashboard PNG — reject it rather than fail silently."""
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError):
            capture._validate_dashboard_path(path)

    @pytest.mark.parametrize(
        "path",
        [
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
    async def test_attack_vector_paths_rejected(self, path: str) -> None:
        """Each path that could re-point the credentialed engine at an
        arbitrary URL / admin route must raise, not just root/empty."""
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError):
            capture._validate_dashboard_path(path)

    async def test_path_segments_are_percent_encoded(self, monkeypatch: Any) -> None:
        """A valid segment with an encodable char reaches the engine
        percent-encoded (defense-in-depth), not raw."""
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(url="http://engine:10000")

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        await capture.capture_dashboard_images("my dash/0")
        assert _FakeAsyncClient.last_get["url"] == "http://engine:10000/my%20dash/0"
