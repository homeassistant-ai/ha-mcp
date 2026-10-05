"""The webhook relay's connection pool and the in-process server it forwards to."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ._embedded_stubs import FakeSession, install

# Install the HA / aiohttp stubs before importing the component (isort barrier).
install()

import custom_components.ha_mcp_tools.mcp_webhook as mw  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK_ID,
    SERVER_KEEPALIVE_SECONDS,
    WEBHOOK_AUTH_NONE,
)


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
