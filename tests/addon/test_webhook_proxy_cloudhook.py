"""Nabu Casa cloudhook relay through the webhook-proxy app (#2696).

Home Assistant Cloud relays a cloudhook in-process as HA's ``MockRequest`` —
no ``read()``, and the relay returns only ``response.body`` — so the forwarder
reads via ``content`` and buffers an SSE reply instead of streaming it.
"""

from __future__ import annotations

import asyncio
import os
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.src.unit._embedded_stubs import MockRequest

from . import test_webhook_proxy as proxy
from .test_webhook_proxy import _webhook_proxy_variant  # noqa: F401


def _cloudhook_relay_supported() -> bool:
    """Feature-detect whether the CURRENT flavor ships ``cloudhook.py``; stable
    skips until promoted."""
    return os.path.exists(
        os.path.join(proxy.PROXY_ADDON_DIR, proxy.CURRENT["component"], "cloudhook.py")
    )


class TestCloudhookRelay:
    @pytest.fixture
    def mod(self) -> ModuleType:
        return proxy._import_mcp_proxy()

    async def test_cloudhook_reads_body_and_buffers_sse(self, mod: ModuleType) -> None:
        if not _cloudhook_relay_supported():
            pytest.skip("flavor does not relay cloudhooks yet")
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        sse = b"event: message\ndata: {}\n\n"
        upstream = MagicMock()
        upstream.status = 200
        upstream.headers = {"Content-Type": "text/event-stream"}
        upstream.read = AsyncMock(return_value=sse)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=upstream)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.request = MagicMock(return_value=ctx)
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": session,
                "oauth": None,
            }
        }
        request = MockRequest(
            content=body,
            mock_source="cloud",
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        with (
            patch.object(mod.web, "Response") as response_cls,
            patch.object(mod.web, "StreamResponse") as stream_cls,
        ):
            await mod._handle_webhook(hass, "mcp_test", request)

        assert session.request.call_args.kwargs["data"] == body
        stream_cls.assert_not_called()
        kwargs = response_cls.call_args.kwargs
        assert kwargs["status"] == 200
        assert kwargs["body"] == sse
        assert kwargs["headers"]["Content-Type"] == "text/event-stream"

    async def test_cloudhook_reply_that_never_ends_is_cut_off_with_504(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _cloudhook_relay_supported():
            pytest.skip("flavor does not relay cloudhooks yet")
        upstream = MagicMock()
        upstream.status = 200
        upstream.headers = {"Content-Type": "text/event-stream"}
        upstream.read = lambda: asyncio.sleep(5)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=upstream)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.request = MagicMock(return_value=ctx)
        hass = MagicMock()
        hass.data = {
            mod.DOMAIN: {
                "target_url": "http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
                "webhook_id": "mcp_test",
                "session": session,
                "oauth": None,
            }
        }
        monkeypatch.setattr(mod.cloudhook, "CLOUDHOOK_REPLY_SECONDS", 0.01)
        request = MockRequest(content=b"{}", mock_source="cloud", method="POST")

        with patch.object(mod.web, "Response") as response_cls:
            await mod._handle_webhook(hass, "mcp_test", request)

        assert response_cls.call_args.kwargs["status"] == 504
