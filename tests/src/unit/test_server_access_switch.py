"""Unit tests for the in-process server's access switch entity (#2801).

The switch platform imports Home Assistant's switch, const and device-registry
modules at import time; the fixture installs minimal stand-ins for them and
imports the platform fresh against those, so other test modules' stubs of the
same names cannot leak in. The bring-up / teardown it drives are patched on
``embedded_setup``, as in ``test_ha_mcp_server_entry``.
"""

from __future__ import annotations

import importlib
import json
import sys
from enum import StrEnum
from pathlib import Path
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

    @property
    def entity_registry_enabled_default(self) -> bool:
        # As Home Assistant's Entity: enabled unless the class attribute says not.
        return getattr(self, "_attr_entity_registry_enabled_default", True)

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
    monkeypatch.setattr(esetup, "async_bring_up_server", MagicMock(name="bring_up"))
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


async def test_the_server_entry_gets_one_access_switch(switch_platform):
    hass, entry = _hass_and_entry()
    added: list = []

    await switch_platform.async_setup_entry(hass, entry, added.extend)

    [switch] = added
    assert isinstance(switch, switch_platform.ServerAccessSwitch)
    assert switch.is_on


def test_the_switch_name_comes_from_the_catalog(switch_platform):
    # A translation key the catalog lacks leaves the entity with no name.
    strings = Path(switch_platform.__file__).with_name("strings.json")
    names = json.loads(strings.read_text(encoding="utf-8"))["entity"]["switch"]

    assert switch_platform.ServerAccessSwitch._attr_translation_key in names


def test_assist_does_not_expose_the_access_switch_by_default(switch_platform):
    # Home Assistant exposes switches to Assist by default unless they carry an
    # entity category; exposed, a voice satellite could turn access back on.
    _hass, entry = _hass_and_entry()

    switch = switch_platform.ServerAccessSwitch(entry)

    assert switch.entity_category is not None


def test_the_access_switch_is_registered_disabled(switch_platform):
    # `entity_id: all` reaches every entity of the switch platform, entity
    # category or not. Home Assistant adds no disabled entity to its platform,
    # so an "everything off" call can close access only once a user enabled it.
    _hass, entry = _hass_and_entry()

    switch = switch_platform.ServerAccessSwitch(entry)

    assert switch.entity_registry_enabled_default is False


async def test_turning_the_switch_on_shows_on(switch_platform):
    hass, entry = _hass_and_entry()
    entry.data = {DATA_ACCESS_ENABLED: False}
    switch = switch_platform.ServerAccessSwitch(entry)
    switch.hass = hass

    await switch.async_turn_on()

    assert switch.written_state is True


async def test_switch_shows_off_when_the_stop_fails(switch_platform):
    # Access is stored off before the stop runs; a failing stop must not leave
    # the switch showing on.
    hass, entry = _hass_and_entry()
    esetup.async_teardown_server.side_effect = RuntimeError("stop failed")
    switch = switch_platform.ServerAccessSwitch(entry)
    switch.hass = hass

    with pytest.raises(RuntimeError):
        await switch.async_turn_off()

    assert switch.written_state is False
