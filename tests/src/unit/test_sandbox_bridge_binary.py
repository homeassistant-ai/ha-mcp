"""Unit tests for the ``binary=True`` response mode on ``_SandboxBridge``.

Covers the fix for sandbox code crashing with an opaque
``UnicodeDecodeError`` when ``api_get``/``api_post`` hit an HA REST
endpoint that streams non-text bytes (e.g. a custom integration's
authenticated file-download view such as Home Keeper's document/manual
endpoint). ``binary=True`` must return base64 instead of attempting
JSON/text decoding; the default path must degrade to a clear error
instead of propagating the raw decode exception.
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.tools_code import _SandboxBridge


class _FakeResponse:
    """Stand-in for an ``httpx.Response``.

    ``json_ok``/``text_ok`` mirror real httpx semantics: ``.json()``
    raises ``json.JSONDecodeError`` on non-JSON bodies, and ``.text``
    raises ``UnicodeDecodeError`` when the declared/sniffed encoding
    can't decode the raw bytes (as it does for real PDF/image content).
    """

    def __init__(
        self, *, content: bytes, content_type: str, json_ok: bool, text_ok: bool
    ):
        self.content = content
        self.headers = {"content-type": content_type}
        self._json_ok = json_ok
        self._text_ok = text_ok

    def json(self):
        if self._json_ok:
            return json.loads(self.content)
        raise json.JSONDecodeError("not json", self.content.decode("latin-1"), 0)

    @property
    def text(self):
        if self._text_ok:
            return self.content.decode("utf-8")
        raise UnicodeDecodeError("utf-8", self.content, 0, 1, "invalid start byte")


def _make_response(
    *, content: bytes, content_type: str, json_ok: bool = False, text_ok: bool = True
):
    return _FakeResponse(
        content=content, content_type=content_type, json_ok=json_ok, text_ok=text_ok
    )


def _make_bridge(response):
    client = SimpleNamespace(
        httpx_client=SimpleNamespace(request=AsyncMock(return_value=response))
    )
    settings = SimpleNamespace(code_mode_max_invocations=100)
    return _SandboxBridge(ctx=None, client=client, settings=settings)


class TestApiGetBinary:
    @pytest.mark.asyncio
    async def test_binary_true_returns_base64_for_pdf(self):
        """A PDF body with binary=True comes back as base64, not a crash."""
        pdf_bytes = b"%PDF-1.4 fake binary \xd3\xff content"
        response = _make_response(
            content=pdf_bytes, content_type="application/pdf", text_ok=False
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/home_keeper/document/asset/doc", binary=True)

        assert "error" not in result
        assert result["content_type"] == "application/pdf"
        assert result["size"] == len(pdf_bytes)
        assert base64.b64decode(result["base64"]) == pdf_bytes

    @pytest.mark.asyncio
    async def test_binary_false_default_still_parses_json(self):
        """binary=False (the default) is unchanged for ordinary JSON APIs."""
        body = b'{"version": "2026.1.0"}'
        response = _make_response(
            content=body, content_type="application/json", json_ok=True
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/config")

        assert result == {"version": "2026.1.0"}

    @pytest.mark.asyncio
    async def test_binary_false_on_binary_body_returns_error_not_crash(self):
        """Without binary=True, a non-text body must degrade to a clear
        error dict — never propagate a raw UnicodeDecodeError to the
        sandbox caller (the bug this test guards against)."""
        pdf_bytes = b"%PDF-1.4 fake binary \xd3\xff content"
        response = _make_response(
            content=pdf_bytes, content_type="application/pdf", text_ok=False
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/home_keeper/document/asset/doc")

        assert "error" in result
        assert "binary=True" in result["error"]
        assert result["content_type"] == "application/pdf"
        assert result["size"] == len(pdf_bytes)


class TestApiPostBinary:
    @pytest.mark.asyncio
    async def test_binary_true_returns_base64(self):
        image_bytes = b"\x89PNG\r\n\x1a\nfake"
        response = _make_response(
            content=image_bytes, content_type="image/png", text_ok=False
        )
        bridge = _make_bridge(response)

        result = await bridge.api_post("/some_endpoint", data={"x": 1}, binary=True)

        assert "error" not in result
        assert result["content_type"] == "image/png"
        assert base64.b64decode(result["base64"]) == image_bytes

    @pytest.mark.asyncio
    async def test_binary_false_default_still_parses_json(self):
        body = b'{"ok": true}'
        response = _make_response(
            content=body, content_type="application/json", json_ok=True
        )
        bridge = _make_bridge(response)

        result = await bridge.api_post("/config/core/check_config")

        assert result == {"ok": True}
