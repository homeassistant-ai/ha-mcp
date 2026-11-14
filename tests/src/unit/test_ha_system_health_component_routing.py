"""``ha_get_system_health`` through the component's snapshot, on a real ``hass``.

Without the component, ``repairs``, ``zwave_network``, ``matter_network`` and
``dead_entities`` each read Core on their own: ``repairs/list_issues``,
``config_entries/get`` once for Z-Wave and once for Matter, and the states,
the entity registry and the config entries again for dead entities, each at
a different instant. When the component advertises ``system_snapshot``, one
``ha_mcp_tools/system_snapshot`` read serves all four (the Z-Wave status call
still runs). Any miss or failure of that read sends every section back to its
own read, never some sections one way and some the other.

Core here holds a Z-Wave and a Matter config entry, a live entity, a stale
restored one, an orphan whose config entry is gone and a repair issue. The
orphan and a restored, inactive issue are what Core loads from storage after
a restart, so the registries are loaded from stored data.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import tools_system
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.tools_system import WS_SYSTEM_SNAPSHOT, SystemTools

from ._component_frames import dropped, held, observed, older_component, pooled

REPAIRS = "repairs/list_issues"
ENTRIES = "config_entries/get"
REGISTRY = "config/entity_registry/list"
SECTIONS = ("repairs", "zwave_network", "matter_network", "dead_entities")
INCLUDE_ALL = ",".join(SECTIONS)
ORPHAN = "sensor.orphan"
# A Z-Wave status call that ran: Z-Wave JS is not set up, so Core answers it
# with an error. "not found" would mean no Z-Wave config entry was seen.
ZWAVE_STATUS_RAN = "Z-Wave JS integration not available or error"


def _stored(key: str, data: dict[str, Any]) -> dict[str, Any]:
    """A storage file at version 1.1; Core migrates it to the current one."""
    return {"version": 1, "minor_version": 1, "key": key, "data": data}


@pytest.fixture
def load_registries() -> bool:
    """``registries`` has Core load them, from what it stored before a restart."""
    return False


@pytest.fixture
async def registries(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    hass_storage[er.STORAGE_KEY] = _stored(
        er.STORAGE_KEY,
        {
            "entities": [
                {
                    "entity_id": ORPHAN,
                    "platform": "zwave_js",
                    "unique_id": "zw-orphan",
                    "config_entry_id": "removed-entry",
                }
            ]
        },
    )
    # A non-persistent issue Core reloads as an inactive stub until its
    # integration raises it again.
    hass_storage[ir.STORAGE_KEY] = _stored(
        ir.STORAGE_KEY,
        {
            "issues": [
                {
                    "created": "2026-01-01T00:00:00+00:00",
                    "dismissed_version": None,
                    "domain": "hue",
                    "issue_id": "iss-restored",
                }
            ]
        },
    )
    dr.async_setup(hass)
    await ar.async_load(hass)
    await cr.async_load(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    await fr.async_load(hass)
    await ir.async_load(hass)
    await lr.async_load(hass)


@pytest.fixture
async def client(
    registries: None, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    for domain in ("config", "repairs", "system_health"):
        assert await async_setup_component(hass, domain, {})
    return component_core_client


class Home(NamedTuple):
    zwave_entry: str
    matter_entry: str
    stale: str


@pytest.fixture
async def home(hass: HomeAssistant, client: HomeAssistantClient) -> Home:
    zwave = MockConfigEntry(domain="zwave_js", title="Z-Wave JS")
    # Neither is set up: their libraries are not installed here, and the
    # sections read only the entries themselves (and the Z-Wave status call).
    matter = MockConfigEntry(domain="matter", title="Matter Hub")
    zwave.add_to_hass(hass)
    matter.add_to_hass(hass)
    registry = er.async_get(hass)
    battery = registry.async_get_or_create(
        "sensor", "zwave_js", "zw-1", config_entry=zwave
    )
    stale = registry.async_get_or_create(
        "light", "matter", "matter-1", config_entry=matter
    )
    hass.states.async_set(battery.entity_id, "100", {"friendly_name": "Battery"})
    # What Core writes for a registry entity no integration has set up.
    hass.states.async_set(stale.entity_id, "unavailable", {"restored": True})
    ir.async_create_issue(
        hass,
        "mqtt",
        "iss-mqtt",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="tk",
    )
    return Home(zwave.entry_id, matter.entry_id, stale.entity_id)


class Reads(NamedTuple):
    """What Core received meanwhile, and the REST states spy."""

    frames: dict[str, list[dict[str, Any]]]
    states: AsyncMock

    def counts(self) -> dict[str, int]:
        return {
            **{command: len(msgs) for command, msgs in self.frames.items()},
            "get_states": self.states.await_count,
        }


@contextmanager
def _reads(hass: HomeAssistant, client: HomeAssistantClient) -> Iterator[Reads]:
    with ExitStack() as stack:
        frames = {
            command: stack.enter_context(observed(hass, command))
            for command in (WS_SYSTEM_SNAPSHOT, REPAIRS, ENTRIES, REGISTRY)
        }
        states = stack.enter_context(
            patch.object(client, "get_states", AsyncMock(wraps=client.get_states))
        )
        yield Reads(frames, states)


def _orphans(resp: dict[str, Any]) -> set[str]:
    items = resp["dead_entities"]["config_entry_orphans"]["items"]
    return {item["entity_id"] for item in items}


def _issue_ids(resp: dict[str, Any]) -> set[str]:
    return {issue["issue_id"] for issue in resp["repairs"]["issues"]}


# --- the snapshot and Core's own reads agree ----------------------------------


async def test_the_snapshot_and_cores_own_reads_give_the_same_sections(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field the snapshot carries differently from Core's own reads would
    change these four sections for every install with the component. The
    repairs include one with no placeholders and one Core hides because its
    config entry is disabled."""
    disabled = MockConfigEntry(domain="hue", disabled_by=ConfigEntryDisabler.USER)
    disabled.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        "hue",
        "iss-disabled-entry",
        config_entry_id=disabled.entry_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="tk",
    )
    tools = SystemTools(client)
    with monkeypatch.context() as older:
        older_component(older, "system_snapshot")
        core = await tools.ha_get_system_health(include=INCLUDE_ALL)
    invalidate_caps(client)

    with observed(hass, WS_SYSTEM_SNAPSHOT) as frames:
        snapshot = await tools.ha_get_system_health(include=INCLUDE_ALL)

    assert len(frames) == 1
    assert snapshot["repairs"] == core["repairs"]
    assert {key: snapshot[key] for key in SECTIONS} == {
        key: core[key] for key in SECTIONS
    }
    assert _issue_ids(snapshot) == {"iss-mqtt"}
    assert _orphans(snapshot) == {ORPHAN}
    stale_items = snapshot["dead_entities"]["stale_restored"]["items"]
    assert {item["entity_id"] for item in stale_items} == {home.stale}
    assert snapshot["matter_network"]["config_entry_id"] == home.matter_entry
    assert snapshot["zwave_network"]["error"].startswith(ZWAVE_STATUS_RAN)


async def test_dismissed_repairs_from_the_snapshot_match_cores_list(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir.async_create_issue(
        hass,
        "hue",
        "iss-dismissed",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="tk",
    )
    ir.async_ignore_issue(hass, "hue", "iss-dismissed", True)
    tools = SystemTools(client)
    with monkeypatch.context() as older:
        older_component(older, "system_snapshot")
        core = await tools.ha_get_system_health(
            include="repairs", include_dismissed_repairs=True
        )
    invalidate_caps(client)

    with observed(hass, REPAIRS) as listed:
        snapshot = await tools.ha_get_system_health(
            include="repairs", include_dismissed_repairs=True
        )

    assert snapshot["repairs"] == core["repairs"]
    assert _issue_ids(snapshot) == {"iss-mqtt", "iss-dismissed"}
    assert snapshot["repairs"]["count"] == 2
    assert "dismissed_count" not in snapshot["repairs"]
    assert listed == []


# --- routing -----------------------------------------------------------------


async def test_one_snapshot_replaces_every_read_it_covers(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    """The Z-Wave status call is not part of the snapshot and still runs; its
    error stays in its own section beside the snapshot-served ones."""
    with _reads(hass, client) as reads:
        resp = await SystemTools(client).ha_get_system_health(include=INCLUDE_ALL)

    assert reads.counts() == {
        WS_SYSTEM_SNAPSHOT: 1,
        REPAIRS: 0,
        ENTRIES: 0,
        REGISTRY: 0,
        "get_states": 0,
    }
    assert resp["zwave_network"]["error"].startswith(ZWAVE_STATUS_RAN)
    assert _issue_ids(resp) == {"iss-mqtt"}
    assert resp["matter_network"] == {
        "config_entry_id": home.matter_entry,
        "state": "not_loaded",
        "title": "Matter Hub",
    }
    assert _orphans(resp) == {ORPHAN}


async def test_a_repairs_only_call_asks_the_snapshot_for_issues_only(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    with observed(hass, WS_SYSTEM_SNAPSHOT) as frames:
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    flags = (
        "include_issues",
        "include_config_entries",
        "include_entities",
        "include_states",
    )
    assert [{flag: frame[flag] for flag in flags} for frame in frames] == [
        {
            "include_issues": True,
            "include_config_entries": False,
            "include_entities": False,
            "include_states": False,
        }
    ]
    assert _issue_ids(resp) == {"iss-mqtt"}


async def test_without_the_snapshot_each_section_reads_core_itself(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z-Wave and Matter each read the config entries, dead entities a third
    time: the reads the snapshot collapses."""
    older_component(monkeypatch, "system_snapshot")

    with _reads(hass, client) as reads:
        resp = await SystemTools(client).ha_get_system_health(include=INCLUDE_ALL)

    assert reads.counts() == {
        WS_SYSTEM_SNAPSHOT: 0,
        REPAIRS: 1,
        ENTRIES: 3,
        REGISTRY: 1,
        "get_states": 1,
    }
    assert _issue_ids(resp) == {"iss-mqtt"}
    assert resp["matter_network"]["config_entry_id"] == home.matter_entry
    assert _orphans(resp) == {ORPHAN}


async def test_a_downgraded_component_sends_every_section_to_core(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await get_component_caps(client) is not None
    monkeypatch.delitem(hass.data[websocket_api.DOMAIN], WS_SYSTEM_SNAPSHOT)

    with (
        patch.object(tools_system, "invalidate_caps", wraps=invalidate_caps) as drop,
        observed(hass, REPAIRS) as repairs,
        observed(hass, ENTRIES) as entries,
    ):
        resp = await SystemTools(client).ha_get_system_health(
            include="repairs,zwave_network"
        )

    assert (len(repairs), len(entries)) == (1, 1)
    assert _issue_ids(resp) == {"iss-mqtt"}
    assert resp["zwave_network"]["error"].startswith(ZWAVE_STATUS_RAN)
    drop.assert_called_once_with(client)


async def test_an_unanswered_snapshot_sends_repairs_to_core_and_keeps_the_component(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    home: Home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch.object(tools_system, "invalidate_caps", wraps=invalidate_caps) as drop,
        observed(hass, REPAIRS) as repairs,
    ):
        async with held(hass, WS_SYSTEM_SNAPSHOT, monkeypatch):
            resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert len(repairs) == 1
    assert _issue_ids(resp) == {"iss-mqtt"}
    drop.assert_not_called()


async def test_a_snapshot_whose_socket_drops_sends_repairs_to_core(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    """Repairs have their own health connection, so a dropped pooled socket
    costs the snapshot, not the section."""
    assert await get_component_caps(client) is not None
    socket = await pooled(client)

    with (
        patch.object(tools_system, "invalidate_caps", wraps=invalidate_caps) as drop,
        observed(hass, REPAIRS) as repairs,
        dropped(hass, WS_SYSTEM_SNAPSHOT, socket),
    ):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert len(repairs) == 1
    assert _issue_ids(resp) == {"iss-mqtt"}
    drop.assert_not_called()


async def test_a_snapshot_with_no_socket_sends_repairs_to_core(
    hass: HomeAssistant, client: HomeAssistantClient, home: Home
) -> None:
    with (
        patch.object(tools_system, "invalidate_caps", wraps=invalidate_caps) as drop,
        patch.object(
            tools_system,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("Failed to connect")),
        ),
        observed(hass, REPAIRS) as repairs,
    ):
        resp = await SystemTools(client).ha_get_system_health(include="repairs")

    assert len(repairs) == 1
    assert _issue_ids(resp) == {"iss-mqtt"}
    drop.assert_not_called()
