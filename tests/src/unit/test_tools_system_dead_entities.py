"""``ha_get_system_health``'s dead_entities on real Home Assistant Core (#1578).

The section diffs Core's entity registry against its states and its config
entries, read without the component: ``get_states``,
``config/entity_registry/list`` and ``config_entries/get``. An entity whose
config entry is gone is what Core loads from storage after a restart, so the
registries load from stored rows; a stale entity carries the restored state
Core writes for a registry entry nothing set up.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, StateMachine
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_system import SystemTools

from ._command_faults import arrival, dropped_on, pooled_socket
from ._component_frames import held

REMOVED_ENTRY = "removed-entry"
ORPHAN = "sensor.orphan"


def _row(entity_id: str, config_entry_id: str | None, **extra: Any) -> dict[str, Any]:
    """An entity registry row as Core stored it."""
    return {
        "entity_id": entity_id,
        "platform": "removed_integration",
        "unique_id": entity_id,
        "config_entry_id": config_entry_id,
        **extra,
    }


@pytest.fixture
def load_registries() -> bool:
    """``registries`` has Core load them from ``stored``, as after a restart."""
    return False


@pytest.fixture
def stored() -> list[dict[str, Any]]:
    """The entity registry rows on disk; a test parametrizes it."""
    return []


@pytest.fixture
async def registries(
    hass: HomeAssistant, hass_storage: dict[str, Any], stored: list[dict[str, Any]]
) -> None:
    hass_storage[er.STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": er.STORAGE_KEY,
        "data": {"entities": stored},
    }
    dr.async_setup(hass)
    for registry in (ar, cr, dr, er, fr, ir, lr):
        await registry.async_load(hass)


@pytest.fixture
async def client(
    registries: None, hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on Core's config commands and health baseline."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "system_health", {})
    return core_client


def _live_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="hue", title="Hue bridge")
    entry.add_to_hass(hass)
    return entry


def _entity(
    hass: HomeAssistant, unique_id: str, entry: MockConfigEntry | None, **kwargs: Any
) -> er.RegistryEntry:
    return er.async_get(hass).async_get_or_create(
        "light", "hue", unique_id, config_entry=entry, **kwargs
    )


def _restored(hass: HomeAssistant, entity_id: str) -> None:
    """The unavailable state Core restores for a registry entry nothing set up."""
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    entry.write_unavailable_state(hass)


async def _dead(client: HomeAssistantClient) -> tuple[dict[str, Any], list[str]]:
    """The section, and the call's top-level warnings."""
    result = await SystemTools(client).ha_get_system_health(include="dead_entities")
    return result["dead_entities"], result.get("warnings", [])


def _ids(bucket: dict[str, Any]) -> list[str]:
    return [item["entity_id"] for item in bucket["items"]]


@pytest.mark.parametrize("stored", [[_row(ORPHAN, REMOVED_ENTRY)]])
async def test_an_entity_whose_integration_was_removed_is_an_orphan(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Live entities of a loaded entry beside it are not; nor is the orphan's
    restored state counted again as stale."""
    entry = _live_entry(hass)
    _entity(hass, "a", entry)
    _entity(hass, "b", entry)
    _restored(hass, ORPHAN)

    dead, _ = await _dead(client)

    (orphan,) = dead["config_entry_orphans"]["items"]
    assert orphan["entity_id"] == ORPHAN
    assert orphan["config_entry_id"] == REMOVED_ENTRY
    assert orphan["has_state"] is True
    assert dead["stale_restored"]["items"] == []
    assert dead["config_entries_checked"] is True
    assert dead["summary"]["candidate_total"] == 1


async def test_a_restored_entity_of_a_loaded_integration_is_stale(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    stale = _entity(hass, "stale", _live_entry(hass))
    _restored(hass, stale.entity_id)

    dead, _ = await _dead(client)

    assert _ids(dead["stale_restored"]) == [stale.entity_id]
    assert dead["config_entry_orphans"]["items"] == []
    assert "ha_remove_entity" in dead["note"]


async def test_a_restored_entity_without_a_config_entry_is_stale(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A YAML entity its platform no longer provides has no entry to lose."""
    _live_entry(hass)
    yaml_entity = _entity(hass, "yaml", None)
    _restored(hass, yaml_entity.entity_id)

    dead, _ = await _dead(client)

    (stale,) = dead["stale_restored"]["items"]
    assert stale["entity_id"] == yaml_entity.entity_id
    assert stale["config_entry_id"] is None
    assert dead["config_entry_orphans"]["items"] == []


@pytest.mark.parametrize(
    ("state", "attributes"),
    [("unknown", {}), ("unavailable", {})],
    ids=["no_value_yet", "offline_now"],
)
async def test_a_live_entity_without_a_value_is_not_flagged(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    state: str,
    attributes: dict[str, Any],
) -> None:
    """A sensor with nothing to report, or a device offline right now, is
    alive: only a state Core restored marks an entity dead."""
    entity = _entity(hass, "alive", _live_entry(hass))
    hass.states.async_set(entity.entity_id, state, attributes)

    dead, warnings = await _dead(client)

    assert dead["summary"] == {"candidate_total": 0, "registry_total": 1}
    assert "note" not in dead
    assert not any("config_entries/get" in w for w in warnings)


async def test_a_disabled_entity_of_a_loaded_integration_is_not_flagged(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _entity(hass, "off", _live_entry(hass), disabled_by=er.RegistryEntryDisabler.USER)

    dead, _ = await _dead(client)

    assert dead["summary"]["candidate_total"] == 0


@pytest.mark.parametrize(
    "stored", [[_row(ORPHAN, REMOVED_ENTRY, disabled_by="integration")]]
)
async def test_a_disabled_entity_whose_integration_was_removed_is_an_orphan(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _live_entry(hass)

    dead, _ = await _dead(client)

    (orphan,) = dead["config_entry_orphans"]["items"]
    assert orphan["disabled_by"] == "integration"
    assert orphan["has_state"] is False


@pytest.mark.parametrize(
    "stored", [[_row(f"sensor.orphan_{i}", REMOVED_ENTRY) for i in range(60)]]
)
async def test_too_many_orphans_are_cut_with_a_hint(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _live_entry(hass)

    dead, _ = await _dead(client)

    _assert_cut(dead["config_entry_orphans"], 60)


async def test_too_many_stale_entities_are_cut_with_a_hint(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _live_entry(hass)
    for i in range(60):
        _restored(hass, _entity(hass, f"stale_{i}", entry).entity_id)

    dead, _ = await _dead(client)

    _assert_cut(dead["stale_restored"], 60)
    assert dead["summary"]["candidate_total"] == 60


def _assert_cut(bucket: dict[str, Any], total: int) -> None:
    assert bucket["truncated"] is True
    assert bucket["count"] == len(bucket["items"]) < total == bucket["total_count"]
    assert f"{bucket['count']} of {total}" in bucket["hint"]
    assert "remove" in bucket["hint"].lower()


@pytest.mark.parametrize("stored", [[_row(ORPHAN, "some-entry")]])
async def test_with_no_config_entries_the_orphan_tier_is_skipped_and_said_so(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Core answering no entries at all cannot tell a removed integration
    from an empty install, so nothing is called an orphan."""
    dead, warnings = await _dead(client)

    assert dead["config_entry_orphans"]["items"] == []
    assert dead["config_entries_checked"] is False
    assert "_warnings" not in dead
    (warning,) = [w for w in warnings if "config_entries/get" in w]
    assert "empty list" in warning
    assert "failed" not in warning


async def test_without_cores_config_entries_only_stale_entities_are_reported(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _entity(hass, "stale", _live_entry(hass))
    _restored(hass, stale.entity_id)
    monkeypatch.delitem(hass.data[websocket_api.DOMAIN], "config_entries/get")

    dead, warnings = await _dead(client)

    assert dead["config_entries_checked"] is False
    assert dead["config_entry_orphans"]["items"] == []
    assert _ids(dead["stale_restored"]) == [stale.entity_id]
    assert "_warnings" not in dead
    (warning,) = [w for w in warnings if "config_entries/get" in w]
    assert "failed (" in warning
    assert "Unknown command." in warning


async def test_without_cores_entity_registry_the_section_reports_the_refusal(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core answered: the refusal stays in the section, the call succeeds."""
    monkeypatch.delitem(hass.data[websocket_api.DOMAIN], "config/entity_registry/list")

    result = await SystemTools(client).ha_get_system_health(include="dead_entities")

    assert result["success"] is True
    assert "registry" in result["dead_entities"]["error"]
    assert "Unknown command." in result["dead_entities"]["error"]


async def test_a_failed_states_read_is_reported_in_the_section(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's states endpoint failing is an answer Core cannot be made to give
    here: its state machine raises instead, for the length of the call."""

    def broken(states: StateMachine, *args: Any, **kwargs: Any) -> list[Any]:
        raise HomeAssistantError("state machine unavailable")

    with monkeypatch.context() as failing:
        failing.setattr(StateMachine, "async_all", broken)
        result = await SystemTools(client).ha_get_system_health(include="dead_entities")

    assert result["success"] is True
    assert "states" in result["dead_entities"]["error"]


async def test_dead_entities_are_returned_without_the_health_baseline(
    registries: None, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's ``system_health`` is not set up; the section needs none of it."""
    assert await async_setup_component(hass, "config", {})

    result = await SystemTools(core_client).ha_get_system_health(
        include="dead_entities"
    )

    assert result["baseline_available"] is False
    assert result["dead_entities"]["summary"] == {
        "candidate_total": 0,
        "registry_total": 0,
    }


# --- no answer from Home Assistant -------------------------------------------
# A detector that cannot read Core would report "nothing is dead": the call
# fails with the cause instead (#1624, #1947).


async def test_a_states_read_with_no_answer_fails_the_call(
    client: HomeAssistantClient,
) -> None:
    with (
        patch.object(
            client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("transport dead")),
        ),
        pytest.raises(ToolError) as excinfo,
    ):
        await _dead(client)

    assert json.loads(str(excinfo.value))["error"]["code"] == "CONNECTION_FAILED"


async def test_a_registry_read_whose_connection_drops_fails_the_call(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    socket = await pooled_socket(client)

    with (
        dropped_on(hass, "config/entity_registry/list", socket),
        pytest.raises(ToolError) as excinfo,
    ):
        await _dead(client)

    assert json.loads(str(excinfo.value))["error"]["code"] == "CONNECTION_FAILED"


async def test_an_unanswered_config_entries_read_fails_the_call(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping only the orphan tier would hide that the connection is wedged."""
    with pytest.raises(ToolError) as excinfo:
        async with held(hass, "config_entries/get", monkeypatch):
            await _dead(client)

    assert json.loads(str(excinfo.value))["error"]["code"] == "CONNECTION_TIMEOUT"


async def test_a_cancelled_call_unwinds_instead_of_returning(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with held(hass, "config/entity_registry/list", monkeypatch):
        with arrival(hass, "config/entity_registry/list") as arrived:
            call = asyncio.ensure_future(_dead(client))
            async with asyncio.timeout(5):
                await arrived.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
