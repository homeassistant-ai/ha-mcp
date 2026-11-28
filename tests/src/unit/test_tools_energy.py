"""Energy orchestration keeps native fields, locking and mutation safeguards.

The tool runs on the server's own client connected to a real ``hass`` with
Core's ``energy`` integration: Core validates and normalizes every save and
answers every read, so these tests pin what HA-MCP adds on top (optimistic
locking, previews, post-save reporting) against Core's real
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


async def _save_key(tool: EnergyTools, key: str, value: list[Any]) -> dict[str, Any]:
    """Save one top-level key under its per-key hash from a fresh read."""
    current = await tool.ha_manage_energy_prefs(mode="get")
    return await tool.ha_manage_energy_prefs(
        mode="set",
        config={key: value},
        config_hash={key: current["config_hash_per_key"][key]},
    )


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
async def test_post_save_issues_remain_visible_without_reporting_failed_save(
    core_client: HomeAssistantClient,
) -> None:
    """Core's energy/validate flags the new device's missing entity; the save
    still stands and the issue is reported."""
    result = await _save_key(
        EnergyTools(core_client),
        "device_consumption",
        [_FRIDGE, {"stat_consumption": "sensor.new"}],
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

    result = await _save_key(
        EnergyTools(core_client),
        "device_consumption",
        [_FRIDGE, {"stat_consumption": "sensor.new"}],
    )

    assert result["success"] is True
    assert result["partial"] is True
    assert any("offline" in w for w in result["warnings"])
    assert {"stat_consumption": "sensor.new"} in (await _core_prefs(core_client))[
        "device_consumption"
    ]


@pytest.mark.usefixtures("energy")
async def test_core_save_refusal_keeps_cores_code_and_saves_nothing(
    core_client: HomeAssistantClient,
) -> None:
    """Core's refusal reaches the caller with Core's code and message, not as a
    generic service failure behind the client's "Command failed:" prefix."""
    tool = EnergyTools(core_client)
    before = await _core_prefs(core_client)
    with sent_messages(core_client) as spy, pytest.raises(ToolError) as exc_info:
        await _save_key(tool, "energy_sources", [{"type": "future"}])

    error = str(exc_info.value)
    assert "Got 'future'" in error  # Core's own refusal text
    assert "VALIDATION_INVALID_PARAMETER" in error
    assert '"ha_error_code": "invalid_format"' in error
    assert "Command failed" not in error
    assert _sent(spy, "energy/save_prefs")
    assert not _sent(spy, "energy/validate")
    assert await _core_prefs(core_client) == before


async def test_saved_list_reaches_core_verbatim(
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
        await _save_key(EnergyTools(core_client), "energy_sources", [existing, new])

    assert _sent(spy, "energy/save_prefs")[0]["energy_sources"] == [existing, new]
    assert (await _core_prefs(core_client))["energy_sources"] == [existing, new]


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
    """Core refusing energy/get_prefs (energy not loaded) is an error that keeps
    Core's code."""
    with pytest.raises(ToolError, match="Failed to get energy prefs") as refused:
        await EnergyTools(core_client).ha_manage_energy_prefs(mode="get")

    assert '"ha_error_code": "unknown_command"' in str(refused.value)


@pytest.mark.usefixtures("energy")
async def test_save_without_a_hash_is_refused_and_saves_nothing(
    core_client: HomeAssistantClient,
) -> None:
    """Every list is saved whole, so a save without the caller's lock would
    overwrite a list the caller never read."""
    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="VALIDATION_MISSING_PARAMETER"),
    ):
        await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="set", config={"device_consumption": []}
        )

    assert not _sent(spy, "energy/save_prefs")


@pytest.mark.parametrize(
    ("saved", "submitted", "refused"),
    [
        ([_FRIDGE], [_FRIDGE, _FRIDGE], True),
        (
            [_FRIDGE],
            [
                {"stat_consumption": "sensor.a", "stat_rate": "sensor.p"},
                {"stat_consumption": "sensor.b", "stat_rate": "sensor.p"},
            ],
            True,
        ),
        ([_FRIDGE, _FRIDGE], [_FRIDGE, _FRIDGE], False),
        ([_FRIDGE], [_FRIDGE, {"stat_consumption": "sensor.new"}], False),
    ],
    ids=["meter_twice", "power_twice", "already_saved_twice", "distinct"],
)
async def test_a_device_list_cannot_gain_a_meter_used_twice(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
    saved: list[dict[str, Any]],
    submitted: list[dict[str, Any]],
    refused: bool,
) -> None:
    """Core saves a device list that uses one meter or power sensor twice;
    Settings > Energy never builds one, so the tool refuses a new repeat and
    leaves an existing one alone."""
    await _set_up_energy(hass, hass_storage, {**_PREFS, "device_consumption": saved})
    tool = EnergyTools(core_client)

    with sent_messages(core_client) as spy:
        if refused:
            with pytest.raises(ToolError, match="VALIDATION_INVALID_PARAMETER"):
                await _save_key(tool, "device_consumption", submitted)
        else:
            await _save_key(tool, "device_consumption", submitted)

    assert bool(_sent(spy, "energy/save_prefs")) is not refused


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (
            {"statistic_ids": ["sensor.x"], "dry_run": True},
            "VALIDATION_INVALID_PARAMETER",
        ),
        ({}, "VALIDATION_MISSING_PARAMETER"),
        ({"statistic_ids": []}, "VALIDATION_MISSING_PARAMETER"),
    ],
    ids=["dry_run", "no_ids", "empty_ids"],
)
async def test_suggestion_change_without_ids_or_with_a_preview_is_refused(
    component_core_client: HomeAssistantClient,
    arguments: dict[str, Any],
    code: str,
) -> None:
    """A dismiss or restore has no preview and needs the meters to change."""
    with pytest.raises(ToolError, match=code):
        await EnergyTools(component_core_client).ha_manage_energy_prefs(
            mode="dismiss_suggestions", **arguments
        )
