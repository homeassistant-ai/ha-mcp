"""Unit tests for the server entry-point wiring (issue #1715 auto-update).

Focuses on the periodic channel auto-update interval that
``async_setup_server_entry`` registers: it must be scheduled on
``UPDATE_CHECK_INTERVAL``, cleaned up on unload, and forward to
``async_check_for_update``.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before the component module binds them). The lazily
imported ``embedded_setup`` / ``ui_panel`` collaborators are replaced with fake
modules so the entry-point wiring is exercised in isolation.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.embedded_entry as eentry  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    UPDATE_CHECK_INTERVAL,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock(name="entry")
    entry.options = {}
    entry.data = {DATA_SECRET_PATH: "/private_x", DATA_WEBHOOK_ID: "mcp_x"}
    entry.entry_id = "entry-1"
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock(name="listener"))
    # Non-coroutine so the (unused) bring-up arg never warns "never awaited".
    entry.async_create_background_task = MagicMock()
    return entry


@pytest.fixture
def fake_collaborators(monkeypatch):
    """Inject fake ``embedded_setup`` / ``ui_panel`` modules.

    ``async_setup_server_entry`` imports ``async_bring_up_server`` /
    ``async_check_for_update`` from ``embedded_setup`` and
    ``async_register_ui_panel`` from ``ui_panel`` at call time; the fakes keep
    the full HA chain out of this test and expose the check as an AsyncMock the
    scheduled callback can be asserted against.
    """
    fake_setup = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
    fake_setup.async_bring_up_server = MagicMock(
        name="async_bring_up_server", return_value=MagicMock(name="bringup_task_arg")
    )
    fake_setup.async_check_for_update = AsyncMock(name="async_check_for_update")

    fake_panel = ModuleType("custom_components.ha_mcp_tools.ui_panel")
    fake_panel.async_register_ui_panel = AsyncMock(name="async_register_ui_panel")

    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.embedded_setup", fake_setup
    )
    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.ui_panel", fake_panel
    )
    return fake_setup


async def test_setup_registers_periodic_update_check(monkeypatch, fake_collaborators):
    hass = _make_hass()
    entry = _make_entry()
    track = MagicMock(
        name="async_track_time_interval", return_value=MagicMock(name="cancel_interval")
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.event"],
        "async_track_time_interval",
        track,
    )

    result = await eentry.async_setup_server_entry(hass, entry)
    assert result is True

    # Registered on the auto-update interval...
    track.assert_called_once()
    args = track.call_args.args
    assert args[0] is hass
    assert args[2] == UPDATE_CHECK_INTERVAL
    # ...and its cancel callback handed to async_on_unload for cleanup.
    cancel = track.return_value
    unload_args = [c.args[0] for c in entry.async_on_unload.call_args_list]
    assert cancel in unload_args


async def test_scheduled_callback_forwards_to_update_check(
    monkeypatch, fake_collaborators
):
    hass = _make_hass()
    entry = _make_entry()
    captured: dict = {}

    def _track(h, action, interval):
        captured["action"] = action
        return MagicMock(name="cancel_interval")

    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.event"],
        "async_track_time_interval",
        _track,
    )

    await eentry.async_setup_server_entry(hass, entry)

    # The registered callback (called by HA with the fire time) forwards to
    # async_check_for_update(hass, entry).
    await captured["action"](None)
    fake_collaborators.async_check_for_update.assert_awaited_once_with(hass, entry)
