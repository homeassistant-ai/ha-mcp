"""Unit tests for the in-process server's access switch entity (#2801).

The switch platform imports Home Assistant's switch, const and device-registry
modules at import time; the fixture installs minimal stand-ins for them and
imports the platform fresh against those, so other test modules' stubs of the
same names cannot leak in. The bring-up / teardown it drives are patched on
``embedded_setup``, as in ``test_ha_mcp_server_entry``.
"""

from __future__ import annotations

import importlib
import sys
from enum import StrEnum
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.embedded_setup as esetup  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_ACCESS_ENABLED,
    DOMAIN,
)


class _EntityCategory(StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class _SwitchEntity:
    hass: MagicMock

    @property
    def entity_category(self) -> _EntityCategory | None:
        # As Home Assistant's Entity: the class attribute backs the property.
        return getattr(self, "_attr_entity_category", None)

    def async_write_ha_state(self) -> None:
        self.written_state = self.is_on  # type: ignore[attr-defined]


@pytest.fixture
def switch_platform(monkeypatch):
    switch_mod = ModuleType("homeassistant.components.switch")
    switch_mod.SwitchEntity = _SwitchEntity  # type: ignore[attr-defined]
    const_mod = ModuleType("homeassistant.const")
    const_mod.EntityCategory = _EntityCategory  # type: ignore[attr-defined]
    dr_mod = ModuleType("homeassistant.helpers.device_registry")
    dr_mod.DeviceInfo = dict  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "homeassistant.components.switch", switch_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", dr_mod)
    monkeypatch.delitem(
        sys.modules, "custom_components.ha_mcp_tools.switch", raising=False
    )
    monkeypatch.setattr(esetup, "async_teardown_server", AsyncMock(name="teardown"))
    monkeypatch.setattr(esetup, "dismiss_connect_notification", MagicMock())
    return importlib.import_module("custom_components.ha_mcp_tools.switch")


def _hass_and_entry() -> tuple[MagicMock, MagicMock]:
    hass = MagicMock(name="hass")
    hass.data = {DOMAIN: {}}

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1"
    entry.data = {}
    return hass, entry


def test_assist_does_not_expose_the_access_switch_by_default(switch_platform):
    # Home Assistant exposes switches to Assist by default unless they carry an
    # entity category; exposed, a voice satellite could turn access back on.
    _hass, entry = _hass_and_entry()

    switch = switch_platform.ServerAccessSwitch(entry)

    assert switch.entity_category is not None


async def test_turning_the_switch_off_closes_access_and_shows_off(switch_platform):
    hass, entry = _hass_and_entry()
    switch = switch_platform.ServerAccessSwitch(entry)
    switch.hass = hass
    assert switch.is_on

    await switch.async_turn_off()

    esetup.async_teardown_server.assert_awaited_once_with(hass)
    assert entry.data[DATA_ACCESS_ENABLED] is False
    assert switch.written_state is False
