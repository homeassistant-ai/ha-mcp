"""Energy orchestration keeps native fields, locking and mutation safeguards.

The tool runs on the server's own client connected to a real ``hass`` with
Core's ``energy`` integration: Core validates and normalizes every save and
answers every read, so these tests pin what HA-MCP adds on top (optimistic
locking, duplicate guards, previews, post-save reporting) against Core's real
behaviour rather than a second schema in a fake server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.energy import websocket_api as energy_ws
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_energy
from ha_mcp.tools.tools_energy import EnergyTools
from ha_mcp.utils.config_hash import compute_config_hash

from ._client_spies import sent_messages

_FRIDGE = {"stat_consumption": "sensor.fridge"}
_PREFS: dict[str, Any] = {
    "energy_sources": [],
    "device_consumption": [_FRIDGE],
    "device_consumption_water": [],
}


async def _set_up_energy(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    prefs: dict[str, Any] | None = None,
) -> None:
    """Core's energy integration, holding ``prefs`` as its saved preferences."""
    if prefs is not None:
        hass_storage["energy"] = {
            "version": 1,
            "minor_version": 3,
            "key": "energy",
            "data": prefs,
        }
    assert await async_setup_component(hass, "energy", {})


async def _core_prefs(client: HomeAssistantClient) -> dict[str, Any]:
    reply = await client.send_websocket_message({"type": "energy/get_prefs"})
    assert reply["success"], reply
    prefs: dict[str, Any] = reply["result"]
    return prefs


async def _save_elsewhere(client: HomeAssistantClient, **prefs: Any) -> None:
    """Another client saves preferences directly in Core."""
    reply = await client.send_websocket_message({"type": "energy/save_prefs", **prefs})
    assert reply["success"], reply


def _sent(spy: AsyncMock, command: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in spy.call_args_list if c.args[0]["type"] == command]


@pytest.fixture
async def energy(
    recorder_mock: Recorder, hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Core's energy integration with ``_PREFS`` saved."""
    await _set_up_energy(hass, hass_storage, _PREFS)


async def test_get_preserves_and_hashes_every_native_field(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """A preference slot this repository does not know (stored by another Core
    version) is returned and gets its own per-key hash: no hardcoded slots."""
    await _set_up_energy(
        hass, hass_storage, {**_PREFS, "future_preference": {"nested": "native"}}
    )

    result = await EnergyTools(core_client).ha_manage_energy_prefs(mode="get")

    assert result["config"] == await _core_prefs(core_client)
    assert result["config"]["future_preference"] == {"nested": "native"}
    assert set(result["config_hash_per_key"]) == set(result["config"])


@pytest.mark.usefixtures("energy")
async def test_save_uses_native_normalized_state_for_next_hash(
    core_client: HomeAssistantClient,
) -> None:
    tool = EnergyTools(core_client)
    before = await tool.ha_manage_energy_prefs(mode="get")
    grid = {
        "type": "grid",
        "stat_energy_from": "sensor.grid",
        "cost_adjustment_day": "1",
    }

    with sent_messages(core_client) as spy:
        result = await tool.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [grid]},
            config_hash=before["config_hash"],
        )
    after = await tool.ha_manage_energy_prefs(mode="get")

    # Core coerced and defaulted the source; the next hash follows Core's state.
    assert result["config"]["energy_sources"][0] != grid
    assert result["config"] == after["config"]
    assert result["config_hash"] == after["config_hash"]
    assert result["config_hash_per_key"] == after["config_hash_per_key"]
    assert _sent(spy, "energy/save_prefs") == [
        {"type": "energy/save_prefs", "energy_sources": [grid]}
    ]


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize("per_key", [False, True])
async def test_stale_hash_never_saves(
    core_client: HomeAssistantClient, per_key: bool
) -> None:
    tool = EnergyTools(core_client)
    before = await tool.ha_manage_energy_prefs(mode="get")
    await _save_elsewhere(core_client, device_consumption=[])
    lock = (
        {"device_consumption": before["config_hash_per_key"]["device_consumption"]}
        if per_key
        else before["config_hash"]
    )

    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="RESOURCE_LOCKED"),
    ):
        await tool.ha_manage_energy_prefs(
            mode="set", config={"device_consumption": [_FRIDGE]}, config_hash=lock
        )

    assert not _sent(spy, "energy/save_prefs")


@pytest.mark.usefixtures("energy")
async def test_per_key_lock_preserves_unrelated_concurrent_change(
    core_client: HomeAssistantClient,
) -> None:
    tool = EnergyTools(core_client)
    before = await tool.ha_manage_energy_prefs(mode="get")
    concurrent = [{"stat_consumption": "sensor.tap"}]
    await _save_elsewhere(core_client, device_consumption_water=concurrent)

    await tool.ha_manage_energy_prefs(
        mode="set",
        config={"device_consumption": []},
        config_hash={
            "device_consumption": before["config_hash_per_key"]["device_consumption"]
        },
    )

    prefs = await _core_prefs(core_client)
    assert prefs["device_consumption"] == []
    assert prefs["device_consumption_water"] == concurrent


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize(
    ("config", "lock"),
    [
        ({}, {}),
        ({"device_consumption": []}, {}),
        ({"device_consumption": []}, {"device_consumption": "x", "extra": "x"}),
    ],
    ids=["nothing_submitted", "key_without_lock", "lock_without_key"],
)
async def test_per_key_locks_cover_exactly_the_nonempty_submission(
    core_client: HomeAssistantClient, config: dict[str, Any], lock: dict[str, str]
) -> None:
    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="VALIDATION_FAILED"),
    ):
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set", config=config, config_hash=lock
        )

    assert not _sent(spy, "energy/save_prefs")


async def test_absent_key_cannot_be_authorized_with_a_fabricated_empty_hash(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """On a never-configured install the key is absent: a hash of an empty value
    made up by the caller is not a lock on it."""
    await _set_up_energy(hass, hass_storage)
    fabricated = {"device_consumption": compute_config_hash({"device_consumption": []})}

    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="RESOURCE_LOCKED"),
    ):
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set", config={"device_consumption": []}, config_hash=fabricated
        )

    assert not _sent(spy, "energy/save_prefs")


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize("field", ["id", "type"])
async def test_config_cannot_override_command_envelope(
    core_client: HomeAssistantClient, field: str
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError, match="reserved"):
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set", config={field: "call_service"}, config_hash="anything"
        )

    spy.assert_not_awaited()


@pytest.mark.usefixtures("energy")
async def test_missing_component_preview_never_claims_validity(
    core_client: HomeAssistantClient,
) -> None:
    with sent_messages(core_client) as spy:
        result = await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set", config={"device_consumption": []}, dry_run=True
        )

    assert result["proposal_validation"]["status"] == "unavailable"
    assert result["partial"] is True
    assert result["warnings"]
    assert not _sent(spy, "energy/save_prefs")


@pytest.mark.usefixtures("energy")
async def test_component_preview_validates_with_cores_schema(
    component_core_client: HomeAssistantClient,
) -> None:
    """With the component, a preview Core accepts is reported validated."""
    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="set", config={"device_consumption": []}, dry_run=True
    )

    assert result["proposal_validation"]["status"] == "validated"
    assert result["proposal_validation"]["valid"] is True
    assert "partial" not in result


@pytest.mark.usefixtures("energy")
async def test_native_proposal_rejection_prevents_convenience_preview(
    component_core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core's schema sees the whole mutated list, and its rejection stops the
    preview; nothing is saved."""
    validator = AsyncMock(wraps=tools_energy.validate_energy_proposal)
    monkeypatch.setattr(tools_energy, "validate_energy_proposal", validator)
    invalid = {"type": "solar"}  # Core requires stat_energy_from

    with (
        sent_messages(component_core_client) as spy,
        pytest.raises(ToolError, match="VALIDATION_FAILED"),
    ):
        await EnergyTools(component_core_client).ha_manage_energy_prefs(
            mode="add_source", source=invalid, dry_run=True
        )

    assert validator.call_args.args[1] == {"energy_sources": [invalid]}
    assert not _sent(spy, "energy/save_prefs")


@pytest.mark.usefixtures("energy")
async def test_whole_mutated_list_is_previewed(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = AsyncMock(wraps=tools_energy.validate_energy_proposal)
    monkeypatch.setattr(tools_energy, "validate_energy_proposal", validator)

    await EnergyTools(core_client).ha_manage_energy_prefs(
        mode="add_device", stat_consumption="sensor.new", dry_run=True
    )

    assert validator.call_args.args[1] == {
        "device_consumption": [_FRIDGE, {"stat_consumption": "sensor.new"}]
    }


@pytest.mark.usefixtures("energy")
async def test_post_save_issues_remain_visible_without_reporting_failed_save(
    core_client: HomeAssistantClient,
) -> None:
    """Core's energy/validate flags the new device's missing entity; the save
    still stands and the issue is reported."""
    result = await EnergyTools(core_client).ha_manage_energy_prefs(
        mode="add_device", stat_consumption="sensor.new"
    )

    assert result["success"] is True
    assert result["warnings"]
    assert "sensor.new" in str(result["post_save_validation_errors"])
    assert {"stat_consumption": "sensor.new"} in (await _core_prefs(core_client))[
        "device_consumption"
    ]


@pytest.mark.usefixtures("energy")
async def test_post_save_validate_failure_remains_visible_without_failing_save(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Core's validation fails; Core answers energy/validate with an error.
    monkeypatch.setattr(
        energy_ws,
        "async_validate",
        AsyncMock(side_effect=HomeAssistantError("offline")),
    )

    result = await EnergyTools(core_client).ha_manage_energy_prefs(
        mode="add_device", stat_consumption="sensor.new"
    )

    assert result["success"] is True
    assert result["partial"] is True
    assert any("offline" in w for w in result["warnings"])
    assert {"stat_consumption": "sensor.new"} in (await _core_prefs(core_client))[
        "device_consumption"
    ]


@pytest.mark.usefixtures("energy")
async def test_core_save_rejection_is_not_silently_accepted(
    core_client: HomeAssistantClient,
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError) as exc_info:
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="add_source", source={"type": "future"}
        )

    assert "Failed to save energy prefs" in str(exc_info.value)
    assert _sent(spy, "energy/save_prefs")
    assert not _sent(spy, "energy/validate")
    assert await _core_prefs(core_client) == _PREFS


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize("water", [False, True])
async def test_device_convenience_roundtrip_preserves_siblings(
    core_client: HomeAssistantClient, water: bool
) -> None:
    tool = EnergyTools(core_client)
    before = await _core_prefs(core_client)
    await tool.ha_manage_energy_prefs(
        mode="add_device",
        stat_consumption="sensor.new",
        name="New",
        included_in_stat="sensor.parent",
        water=water,
    )
    key = "device_consumption_water" if water else "device_consumption"
    assert (await _core_prefs(core_client))[key][-1] == {
        "stat_consumption": "sensor.new",
        "name": "New",
        "included_in_stat": "sensor.parent",
    }

    await tool.ha_manage_energy_prefs(
        mode="remove_device", stat_consumption="sensor.new", water=water
    )

    assert await _core_prefs(core_client) == before


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("mode", "kwargs", "code"),
    [
        (
            "add_device",
            {"stat_consumption": "sensor.fridge"},
            "RESOURCE_ALREADY_EXISTS",
        ),
        ("remove_device", {"stat_consumption": "sensor.absent"}, "RESOURCE_NOT_FOUND"),
        ("add_device", {}, "VALIDATION_MISSING_PARAMETER"),
        ("remove_device", {}, "VALIDATION_MISSING_PARAMETER"),
        ("add_source", {}, "VALIDATION_MISSING_PARAMETER"),
    ],
)
async def test_invalid_convenience_operation_never_saves(
    core_client: HomeAssistantClient,
    dry_run: bool,
    mode: str,
    kwargs: dict[str, Any],
    code: str,
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError, match=code):
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode=mode, dry_run=dry_run, **kwargs
        )

    assert not _sent(spy, "energy/save_prefs")


async def test_added_source_reaches_core_verbatim_and_existing_siblings_survive(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    existing = {
        "type": "battery",
        "stat_energy_from": "sensor.battery_out",
        "stat_energy_to": "sensor.battery_in",
        "capacity": 10.0,
    }
    await _set_up_energy(hass, hass_storage, {**_PREFS, "energy_sources": [existing]})
    new = {
        "type": "solar",
        "stat_energy_from": "sensor.solar",
        "config_entry_solar_forecast": None,
    }

    with sent_messages(core_client) as spy:
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="add_source", source=new
        )

    assert _sent(spy, "energy/save_prefs")[0]["energy_sources"] == [existing, new]
    assert (await _core_prefs(core_client))["energy_sources"] == [existing, new]


@pytest.mark.usefixtures("energy")
@pytest.mark.parametrize(
    ("source", "duplicate"),
    [
        (
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_out",
                "stat_energy_to": "sensor.battery_in",
            },
            True,
        ),
        (
            {
                "type": "grid",
                "stat_energy_from": "sensor.grid",
                "cost_adjustment_day": 0,
            },
            False,
        ),
    ],
    ids=["battery", "grid"],
)
async def test_source_duplicate_guard_is_a_wrapper_policy(
    core_client: HomeAssistantClient, source: dict[str, Any], duplicate: bool
) -> None:
    """A second identical non-grid source is refused by the wrapper before Core
    sees it; a grid source is never refused by the wrapper, Core decides."""
    tool = EnergyTools(core_client)
    await tool.ha_manage_energy_prefs(mode="add_source", source=source)

    with sent_messages(core_client) as spy, pytest.raises(ToolError) as exc_info:
        await tool.ha_manage_energy_prefs(mode="add_source", source=source)

    refused_by_wrapper = "RESOURCE_ALREADY_EXISTS" in str(exc_info.value)
    assert refused_by_wrapper is duplicate
    assert bool(_sent(spy, "energy/save_prefs")) is not duplicate
    assert len((await _core_prefs(core_client))["energy_sources"]) == 1


@pytest.mark.usefixtures("energy")
async def test_convenience_preview_does_not_write(
    core_client: HomeAssistantClient,
) -> None:
    with sent_messages(core_client) as spy:
        result = await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="add_device", stat_consumption="sensor.new", dry_run=True
        )

    assert result["new_count"] == 2
    assert result["proposal_validation"]["status"] == "unavailable"
    assert await _core_prefs(core_client) == _PREFS
    assert not _sent(spy, "energy/save_prefs")


async def test_never_configured_install_reads_as_empty_prefs(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """Core's "No prefs" (never configured) is not a read failure."""
    await _set_up_energy(hass, hass_storage)

    result = await EnergyTools(core_client).ha_manage_energy_prefs(mode="get")

    assert result["config"] == {}
    assert "never been configured" in result["note"]
    assert "unavailable" in result["note"]


async def test_read_failure_is_not_mistaken_for_unconfigured(
    core_client: HomeAssistantClient,
) -> None:
    """Core refusing energy/get_prefs (energy not loaded) is an error."""
    with pytest.raises(ToolError, match="Failed to get energy prefs"):
        await EnergyTools(core_client).ha_manage_energy_prefs(mode="get")
