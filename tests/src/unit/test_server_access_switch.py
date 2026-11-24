"""The in-process server's access switch entity (#2801), on a real ``hass``.

Home Assistant sets the server entry up and adds the switch through its own
entity platform; only the server bring-up / teardown are replaced.
"""

from __future__ import annotations

import builtins
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from custom_components.ha_mcp_tools import switch as switch_platform
from custom_components.ha_mcp_tools.const import (
    DATA_ACCESS_ENABLED,
    SERVER_CONFIG_SUBDIR,
)
from custom_components.ha_mcp_tools.embedded_read_only import set_read_only_mode
from ha_mcp.read_only import read_only_remedy_hint
from homeassistant.components.switch.const import DATA_COMPONENT
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed

from ._server_entry import add_server_entry, replace_server, set_up


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return replace_server(monkeypatch)


async def _switch_entry(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, **data: object
) -> er.RegistryEntry:
    """The access switch's registry entry after Home Assistant set the entry up."""
    entry = add_server_entry(hass, data=dict(data))
    assert await set_up(hass, entry)
    return next(
        registered
        for registered in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if registered.unique_id == f"{entry.entry_id}_server_access"
    )


async def _read_only_mode_entry(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> er.RegistryEntry:
    """The Read Only Mode switch after Home Assistant set the entry up."""
    entry = add_server_entry(hass)
    assert await set_up(hass, entry)
    return next(
        registered
        for registered in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if registered.unique_id == f"{entry.entry_id}_server_read_only_mode"
    )


def _feature_flags_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(SERVER_CONFIG_SUBDIR, "feature_flags.json"))


def _shown(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def test_read_only_write_never_cold_imports_the_server_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HA executor must not join a worker's in-progress ha_mcp import."""
    real_import = builtins.__import__

    def reject_server_import(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ):
        if name == "ha_mcp" or name.startswith("ha_mcp."):
            raise AssertionError(f"component write imported {name}")
        return real_import(name, globals_, locals_, fromlist, level)

    with monkeypatch.context() as import_guard:
        import_guard.setattr(builtins, "__import__", reject_server_import)
        set_read_only_mode(tmp_path, True)
    assert json.loads((tmp_path / "feature_flags.json").read_text()) == {
        "read_only_mode": True
    }


def test_embedded_remedy_names_the_device_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ha_mcp.read_only.get_global_settings",
        lambda: SimpleNamespace(read_only_mode=True),
    )
    monkeypatch.setattr("ha_mcp.read_only.is_embedded_mode", lambda: True)

    hint = read_only_remedy_hint()

    assert "HA-MCP Server device" in hint
    assert "Read Only Mode switch" in hint


async def test_the_server_entry_gets_one_access_switch(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    registered = await _switch_entry(hass, entity_registry)

    assert registered.domain == "switch"
    state = hass.states.get(registered.entity_id)
    assert state is not None
    assert state.state == STATE_ON


async def test_the_switch_name_comes_from_the_catalog(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, server: SimpleNamespace
) -> None:
    # A translation key the catalog lacks leaves the entity with no name.
    registered = await _switch_entry(hass, entity_registry)
    strings = Path(switch_platform.__file__).with_name("strings.json")
    names = json.loads(strings.read_text(encoding="utf-8"))["entity"]["switch"]

    assert registered.translation_key in names


async def test_the_read_only_mode_name_comes_from_the_catalog(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, server: SimpleNamespace
) -> None:
    registered = await _read_only_mode_entry(hass, entity_registry)
    strings = Path(switch_platform.__file__).with_name("strings.json")
    names = json.loads(strings.read_text(encoding="utf-8"))["entity"]["switch"]

    assert registered.translation_key in names


async def test_assist_does_not_expose_the_access_switch_by_default(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, server: SimpleNamespace
) -> None:
    # Home Assistant exposes switches to Assist by default unless they carry an
    # entity category; exposed, a voice satellite could turn access back on.
    registered = await _switch_entry(hass, entity_registry)

    assert registered.entity_category is not None


async def test_an_everything_off_call_cannot_close_access_on_a_default_install(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, server: SimpleNamespace
) -> None:
    # `entity_id: all` reaches every entity of the switch platform, entity
    # category or not. Home Assistant adds no disabled entity to its platform,
    # so an "everything off" call can close access only once a user enabled it.
    registered = await _switch_entry(hass, entity_registry)

    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(registered.entity_id) is None


async def test_read_only_mode_is_opt_in_and_hidden_from_assist(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, server: SimpleNamespace
) -> None:
    registered = await _read_only_mode_entry(hass, entity_registry)

    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registered.entity_category is not None
    assert hass.states.get(registered.entity_id) is None


async def test_read_only_mode_defaults_off(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    registered = await _read_only_mode_entry(hass, entity_registry)

    assert _shown(hass, registered.entity_id) == STATE_OFF


async def test_read_only_mode_reads_the_panel_setting(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"read_only_mode": True}), encoding="utf-8")

    registered = await _read_only_mode_entry(hass, entity_registry)

    assert _shown(hass, registered.entity_id) == STATE_ON


async def test_read_only_mode_matches_server_integer_override(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"read_only_mode": 1}), encoding="utf-8")

    registered = await _read_only_mode_entry(hass, entity_registry)

    assert _shown(hass, registered.entity_id) == STATE_ON


async def test_turning_read_only_mode_on_preserves_other_settings(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"enable_tool_search": True}), encoding="utf-8")
    registered = await _read_only_mode_entry(hass, entity_registry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": registered.entity_id}, blocking=True
    )

    assert _shown(hass, registered.entity_id) == STATE_ON
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enable_tool_search": True,
        "read_only_mode": True,
    }


async def test_read_only_mode_refreshes_after_a_panel_change(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    registered = await _read_only_mode_entry(hass, entity_registry)
    assert _shown(hass, registered.entity_id) == STATE_OFF
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"read_only_mode": True}), encoding="utf-8")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()

    assert _shown(hass, registered.entity_id) == STATE_ON


@pytest.mark.parametrize("payload", [b"\xff", b"[]", b"{not json"])
async def test_read_only_mode_refuses_to_replace_an_invalid_override(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
    payload: bytes,
) -> None:
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    registered = await _read_only_mode_entry(hass, entity_registry)
    entity = hass.data[DATA_COMPONENT].get_entity(registered.entity_id)
    assert entity is not None

    with pytest.raises(HomeAssistantError, match="Could not change Read Only Mode"):
        await entity.async_turn_on()

    assert path.read_bytes() == payload


@pytest.mark.parametrize("env_name", ["READ_ONLY_MODE", "read_only_mode"])
async def test_read_only_mode_environment_lock_is_friendly_and_removable(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "y")
    registered = await _read_only_mode_entry(hass, entity_registry)
    path = _feature_flags_path(hass)

    assert _shown(hass, registered.entity_id) == STATE_ON
    with pytest.raises(HomeAssistantError, match="locked by the READ_ONLY_MODE"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": registered.entity_id}, blocking=True
        )
    assert not path.exists()

    monkeypatch.delenv(env_name)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": registered.entity_id}, blocking=True
    )
    assert json.loads(path.read_text(encoding="utf-8"))["read_only_mode"] is False


async def test_unreadable_read_only_mode_warns_only_on_transition(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _feature_flags_path(hass)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")
    caplog.set_level(logging.WARNING, logger=switch_platform.__name__)
    registered = await _read_only_mode_entry(hass, entity_registry)

    for seconds in (31, 62):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await hass.async_block_till_done()

    warnings = [
        record
        for record in caplog.records
        if record.message.startswith("Cannot read embedded Read Only Mode")
    ]
    assert len(warnings) == 1
    assert _shown(hass, registered.entity_id) == "unavailable"


async def test_turning_the_switch_on_shows_on(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    registered = await _switch_entry(
        hass, entity_registry, **{DATA_ACCESS_ENABLED: False}
    )
    assert _shown(hass, registered.entity_id) == STATE_OFF

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": registered.entity_id}, blocking=True
    )

    assert _shown(hass, registered.entity_id) == STATE_ON


async def test_switch_shows_off_when_the_stop_fails(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_registry_enabled_by_default: None,
    server: SimpleNamespace,
) -> None:
    # Access is stored off before the stop runs; a failing stop must not leave
    # the switch showing on.
    registered = await _switch_entry(hass, entity_registry)
    server.teardown.side_effect = RuntimeError("stop failed")

    with pytest.raises(RuntimeError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": registered.entity_id}, blocking=True
        )
    server.teardown.side_effect = None

    assert _shown(hass, registered.entity_id) == STATE_OFF
