"""The webhook relay's connection pool and the in-process server it forwards to."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ._embedded_stubs import FakeSession, install, make_request

# Install the HA / aiohttp stubs before importing the component (isort barrier).
install()

import custom_components.ha_mcp_tools.mcp_webhook as mw  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK_ID,
    SERVER_KEEPALIVE_SECONDS,
    WEBHOOK_AUTH_NONE,
)

from .test_mcp_webhook import WEBHOOK_ID, _make_hass, _store_cfg  # noqa: E402


async def test_relay_never_reuses_a_connection_the_server_has_closed(
    monkeypatch,
) -> None:
    """A pooled connection the in-process server had already timed out made
    the next request fail with a connection reset and a 502."""
    sessions: list[dict] = []
    monkeypatch.setattr(
        mw.aiohttp, "ClientSession", lambda **kw: sessions.append(kw) or FakeSession()
    )
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.config = SimpleNamespace(components={"webhook"})
    entry = MagicMock()
    entry.data = {DATA_WEBHOOK_ID: "mcp_0123456789abcdef0123456789abcdef"}

    await mw.async_register_webhook(
        hass,
        entry,
        port=9584,
        secret_path="/private_x",
        auth_mode=WEBHOOK_AUTH_NONE,
    )

    # The value the in-process server passes to uvicorn, not uvicorn's default.
    assert sessions[0]["connector"].keepalive_timeout < SERVER_KEEPALIVE_SECONDS
    await mw.async_unregister_webhook(hass)


async def test_a_full_pool_logs_no_secret_path(caplog) -> None:
    """aiohttp's pool-acquire timeout names the target URL; its path is the
    secret path, so the 502 log line must carry only the error type."""
    err = mw.aiohttp.ClientError("Connection timeout to host http://127.0.0.1/p_s3cr3t")
    hass = _make_hass()
    _store_cfg(hass, session=FakeSession(exc=err))

    resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())

    assert resp.status == 502
    assert "upstream request failed" in caplog.text
    assert "p_s3cr3t" not in caplog.text
