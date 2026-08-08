"""Unit tests for the ``binary=True`` response mode on ``_SandboxBridge``.

Covers the fix for sandbox code crashing with an opaque
``UnicodeDecodeError`` when ``api_get``/``api_post`` hit an HA REST
endpoint that streams non-text bytes (e.g. a custom integration's
authenticated file-download view such as Home Keeper's document/manual
endpoint). ``binary=True`` must return base64 instead of attempting
JSON/text decoding; the default path must degrade to a clear error
instead of propagating the raw decode exception.

Uses real ``httpx.Response`` objects (not a hand-rolled fake) so the
tests exercise actual httpx decode behavior: ``Response.json()``
decodes ``.content`` with a guessed encoding and *no* error
replacement, so a genuinely non-UTF-8 body raises ``UnicodeDecodeError``
there — not ``json.JSONDecodeError`` — while ``Response.text`` falls
back to replacement-decoding and does not raise. A mock that only ever
raises ``JSONDecodeError`` would mask exactly the failure mode this fix
targets.
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp.tools.tools_code import _SandboxBridge

# Real PDF-ish bytes: not valid JSON, and not valid UTF-8 (0xd3 with no
# continuation byte). Mirrors the actual incident this fix addresses.
_PDF_BYTES = b"%PDF-1.4 fake binary \xd3\xff content"
_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake binary \xd3\xff content"


def _make_bridge(response: httpx.Response, *, max_binary_size: int = 10_485_760):
    client = SimpleNamespace(
        httpx_client=SimpleNamespace(request=AsyncMock(return_value=response))
    )
    settings = SimpleNamespace(
        code_mode_max_invocations=100, code_mode_max_memory=max_binary_size
    )
    return _SandboxBridge(ctx=None, client=client, settings=settings)


class TestApiGetBinary:
    @pytest.mark.asyncio
    async def test_binary_true_returns_base64_for_pdf(self):
        """A PDF body with binary=True comes back as base64, not a crash."""
        response = httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/home_keeper/document/asset/doc", binary=True)

        assert "error" not in result
        assert result["content_type"] == "application/pdf"
        assert result["size"] == len(_PDF_BYTES)
        assert base64.b64decode(result["base64"]) == _PDF_BYTES

    @pytest.mark.asyncio
    async def test_binary_true_over_size_cap_returns_error_not_base64(self):
        """A body over CODE_MODE_MAX_MEMORY must not be base64-encoded —
        that would defeat the point of bounding host-side memory use."""
        response = httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES
        )
        bridge = _make_bridge(response, max_binary_size=len(_PDF_BYTES) - 1)

        result = await bridge.api_get("/home_keeper/document/asset/doc", binary=True)

        assert "base64" not in result
        assert "error" in result
        assert "CODE_MODE_MAX_MEMORY" in result["error"]
        assert result["size"] == len(_PDF_BYTES)

    @pytest.mark.asyncio
    async def test_binary_false_default_still_parses_json(self):
        """binary=False (the default) is unchanged for ordinary JSON APIs."""
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"version": "2026.1.0"}',
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/config")

        assert result == {"version": "2026.1.0"}

    @pytest.mark.asyncio
    async def test_binary_false_falls_through_to_text_for_plain_text_endpoints(self):
        """A genuinely non-JSON but valid-UTF-8 body (JSONDecodeError, not
        UnicodeDecodeError) must still fall through to response.text —
        the UnicodeDecodeError-only short-circuit must not swallow this
        legitimate plain-text case."""
        response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"OK, config valid",
        )
        bridge = _make_bridge(response)

        result = await bridge.api_get("/config/core/check_config")

        assert result == "OK, config valid"

    @pytest.mark.asyncio
    async def test_binary_false_on_binary_body_returns_error_not_crash(self):
        """Without binary=True, a non-text body must degrade to a clear
        error dict — never propagate a raw UnicodeDecodeError to the
        sandbox caller (the bug this test guards against). This is the
        exact real-world failure mode: httpx's Response.json() raises
        UnicodeDecodeError (not JSONDecodeError) on non-UTF-8 content."""
        response = httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES
        )
        bridge = _make_bridge(response)

        # Confirm the assumption this test relies on: json() raises
        # UnicodeDecodeError for this body, not JSONDecodeError.
        with pytest.raises(UnicodeDecodeError):
            response.json()

        result = await bridge.api_get("/home_keeper/document/asset/doc")

        assert "error" in result
        assert "binary=True" in result["error"]
        assert result["content_type"] == "application/pdf"
        assert result["size"] == len(_PDF_BYTES)


class TestApiPostBinary:
    @pytest.mark.asyncio
    async def test_binary_true_returns_base64(self):
        response = httpx.Response(
            200, headers={"content-type": "image/png"}, content=_PNG_BYTES
        )
        bridge = _make_bridge(response)

        result = await bridge.api_post("/some_endpoint", data={"x": 1}, binary=True)

        assert "error" not in result
        assert result["content_type"] == "image/png"
        assert base64.b64decode(result["base64"]) == _PNG_BYTES

    @pytest.mark.asyncio
    async def test_binary_false_default_still_parses_json(self):
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"ok": true}',
        )
        bridge = _make_bridge(response)

        result = await bridge.api_post("/config/core/check_config")

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_binary_false_on_binary_body_returns_error_not_crash(self):
        response = httpx.Response(
            200, headers={"content-type": "image/png"}, content=_PNG_BYTES
        )
        bridge = _make_bridge(response)

        result = await bridge.api_post("/some_endpoint", data={"x": 1})

        assert "error" in result
        assert "binary=True" in result["error"]
