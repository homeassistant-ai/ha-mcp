"""The in-process server's access switch entity (#2801), on a real ``hass``.

Home Assistant sets the server entry up and adds the switch through its own
entity platform; only the server bring-up / teardown are replaced.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.ha_mcp_tools import switch as switch_platform
from custom_components.ha_mcp_tools.const import DATA_ACCESS_ENABLED

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
    [registered] = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    return registered


def _shown(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


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
