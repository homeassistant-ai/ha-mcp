"""A small raw tail comes from Core's stream and releases its subscription."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.system_log.stream import DATA_RAW_LOG_STREAM
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client


@pytest.mark.parametrize(
    ("lines", "expected", "more"),
    [(2, ["first", "second"], True), (4, ["startup", "first", "second"], False)],
)
async def test_tail_releases_raw_log_subscription(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    lines: int,
    expected: list[str],
    more: bool,
) -> None:
    path = tmp_path / "core.log"
    path.write_text("startup\nfirst\nsecond\n", encoding="utf-8")
    hass.data[KEY_DATA_LOGGING] = str(path)
    assert await async_setup_component(hass, "system_log", {})
    ws = await get_websocket_client(
        url=core_client.base_url,
        token=core_client.token,
        verify_ssl=core_client.verify_ssl,
    )
    with patch.object(
        ws, "release_subscription", AsyncMock(wraps=ws.release_subscription)
    ) as release:
        page = await core_client.get_error_log(lines=lines)
    assert page.text.splitlines() == expected
    assert page.has_more is more
    release.assert_awaited_once()
    assert hass.data[DATA_RAW_LOG_STREAM] not in logging.getLogger().handlers
