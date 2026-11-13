"""Core's KNX test kit, adapted to the installed Core: the real ``knx``
integration on a real ``hass``, with only the KNX bus below xknx replaced.

This is ``KNXTestKit.setup_integration`` and the ``mock_config_entry`` fixture
from Core's ``tests/components/knx/conftest.py``. As there, xknx's KNX/IP
interface (the tunnelling or routing socket to the bus) is a mock whose
``start`` reports a connected tunnel, and a telegram xknx sends is taken off
before it reaches that interface. Everything above it is Core's and xknx's own
code: the config entry, ``KNXModule``, the config store and every ``knx/*``
WebSocket command with its reply. Unlike the kit, the telegram store is the
real SQLite store, so a test that sets knx up overrides ``hass_config_dir``
with ``hass_tmp_config_dir`` and the database lands in its own directory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.knx.const import (
    CONF_KNX_AUTOMATIC,
    CONF_KNX_CONNECTION_TYPE,
    CONF_KNX_DEFAULT_RATE_LIMIT,
    CONF_KNX_DEFAULT_STATE_UPDATER,
    CONF_KNX_INDIVIDUAL_ADDRESS,
    CONF_KNX_MCAST_GRP,
    CONF_KNX_MCAST_PORT,
    CONF_KNX_RATE_LIMIT,
    CONF_KNX_STATE_UPDATER,
    CONF_KNX_TELEGRAM_DB_BACKEND,
    CONF_KNX_TELEGRAM_DB_LOAD_HOURS,
    CONF_KNX_TELEGRAM_DB_RETENTION_DAYS,
    DEFAULT_ROUTING_IA,
    DOMAIN,
    KNX_TELEGRAM_BACKEND_SQLITE,
    KNX_TELEGRAM_DB_RETENTION_DEFAULT,
    KNX_TELEGRAM_LOAD_HOURS_DEFAULT,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from xknx import XKNX
from xknx.cemi.cemi_handler import CEMIHandler
from xknx.core import XknxConnectionState, XknxConnectionType
from xknx.io import DEFAULT_MCAST_GRP, DEFAULT_MCAST_PORT
from xknx.telegram import Telegram


def _bus_interface(xknx: XKNX, *args: Any, **kwargs: Any) -> Mock:
    """The KNX/IP interface xknx builds; ``start`` connects a tunnel."""

    async def connect() -> None:
        xknx.rate_limit = 0
        xknx.connection_manager.connection_state_changed(
            state=XknxConnectionState.CONNECTED,
            connection_type=XknxConnectionType.TUNNEL_TCP,
        )

    interface = Mock()
    interface.start = AsyncMock(side_effect=connect)
    interface.stop = AsyncMock()
    interface.stop_reconnect = AsyncMock()
    interface.gateway_info = AsyncMock()
    return interface


async def _send_to_no_bus(self: CEMIHandler, telegram: Telegram) -> None:
    """Take an outgoing telegram off before it would wait for the bus."""


async def set_up_knx(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    """Set Core's ``knx`` integration up from its config entry."""
    monkeypatch.setattr("xknx.xknx.knx_interface_factory", _bus_interface)
    monkeypatch.setattr(CEMIHandler, "send_telegram", _send_to_no_bus)
    entry = MockConfigEntry(
        title="KNX",
        domain=DOMAIN,
        version=2,
        data={
            CONF_KNX_CONNECTION_TYPE: CONF_KNX_AUTOMATIC,
            CONF_KNX_MCAST_PORT: DEFAULT_MCAST_PORT,
            CONF_KNX_MCAST_GRP: DEFAULT_MCAST_GRP,
            CONF_KNX_INDIVIDUAL_ADDRESS: DEFAULT_ROUTING_IA,
        },
        options={
            CONF_KNX_RATE_LIMIT: CONF_KNX_DEFAULT_RATE_LIMIT,
            CONF_KNX_STATE_UPDATER: CONF_KNX_DEFAULT_STATE_UPDATER,
            CONF_KNX_TELEGRAM_DB_RETENTION_DAYS: KNX_TELEGRAM_DB_RETENTION_DEFAULT,
            CONF_KNX_TELEGRAM_DB_LOAD_HOURS: KNX_TELEGRAM_LOAD_HOURS_DEFAULT,
            CONF_KNX_TELEGRAM_DB_BACKEND: KNX_TELEGRAM_BACKEND_SQLITE,
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry
