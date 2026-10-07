"""Energy orchestration keeps native fields, locking and mutation safeguards.

Real Core schema acceptance belongs in E2E. These tests intentionally do not
implement a second battery/grid schema in a fake server.
"""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_energy as module
from ha_mcp.tools.energy_statistics import _compute_per_key_hashes
from ha_mcp.tools.tools_energy import EnergyTools
from ha_mcp.utils.config_hash import compute_config_hash


@pytest.fixture
def world() -> tuple[EnergyTools, dict[str, Any]]:
    state: dict[str, Any] = {
        "prefs": {"device_consumption": [{"stat_consumption": "sensor.fridge"}],
                  "energy_sources": [], "device_consumption_water": [],
                  "future_preference": {"nested": "native"}},
        "calls": [],
        "validation": {"success": True, "result": {}},
    }

    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        state["calls"].append(deepcopy(message))
        if message["type"] == "energy/get_prefs":
            return {"success": True, "result": deepcopy(state["prefs"])}
        if message["type"] == "energy/validate":
            return state["validation"]
        if message["type"] == "energy/save_prefs":
            if "save_failure" in state:
                return {"success": False, "error": state["save_failure"]}
            state["prefs"].update({k: v for k, v in message.items() if k != "type"})
            state["prefs"].update(state.get("normalized", {}))
            return {"success": True, "result": deepcopy(state["prefs"])}
        raise AssertionError(f"Unexpected request: {message}")

    client = MagicMock(base_url=None, token=None, send_websocket_message=AsyncMock(side_effect=dispatch))
    return EnergyTools(client), state


def saves(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in state["calls"] if c["type"] == "energy/save_prefs"]


@pytest.mark.asyncio
async def test_get_preserves_and_hashes_future_native_fields(world) -> None:
    tool, state = world
    result = await tool.ha_manage_energy_prefs(mode="get")
    assert result["config"] == state["prefs"]
    assert result["config_hash"] == compute_config_hash(state["prefs"])
    assert result["config_hash_per_key"]["future_preference"] == compute_config_hash({"future_preference": state["prefs"]["future_preference"]})


@pytest.mark.asyncio
async def test_save_uses_native_normalized_state_for_next_hash(world) -> None:
    tool, state = world
    state["normalized"] = {"future_preference": {"coerced": 1.0, "default": "Core"}}
    before = await tool.ha_manage_energy_prefs(mode="get")
    result = await tool.ha_manage_energy_prefs(mode="set", config={"future_preference": {"coerced": "1"}}, config_hash=before["config_hash"])
    after = await tool.ha_manage_energy_prefs(mode="get")
    assert result["config"] == after["config"]
    assert result["config_hash"] == after["config_hash"]
    assert result["config_hash_per_key"] == after["config_hash_per_key"]
    assert saves(state) == [{"type": "energy/save_prefs", "future_preference": {"coerced": "1"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("per_key", [False, True])
async def test_stale_hash_never_saves(world, per_key: bool) -> None:
    tool, state = world
    before = await tool.ha_manage_energy_prefs(mode="get")
    state["prefs"]["device_consumption"] = []
    lock = {"device_consumption": before["config_hash_per_key"]["device_consumption"]} if per_key else before["config_hash"]
    with pytest.raises(ToolError, match="RESOURCE_LOCKED"):
        await tool.ha_manage_energy_prefs(mode="set", config={"device_consumption": []}, config_hash=lock)
    assert not saves(state)


@pytest.mark.asyncio
async def test_per_key_lock_preserves_unrelated_concurrent_change(world) -> None:
    tool, state = world
    before = await tool.ha_manage_energy_prefs(mode="get")
    state["prefs"]["future_preference"] = "changed by someone else"
    await tool.ha_manage_energy_prefs(mode="set", config={"device_consumption": []}, config_hash={"device_consumption": before["config_hash_per_key"]["device_consumption"]})
    assert state["prefs"]["future_preference"] == "changed by someone else"


@pytest.mark.parametrize("config,lock", [
    ({}, {}),
    ({"device_consumption": []}, {}),
    ({"device_consumption": []}, {"device_consumption": "x", "extra": "x"}),
])
def test_per_key_locks_cover_exactly_the_nonempty_submission(config, lock) -> None:
    with pytest.raises(ToolError, match="VALIDATION_FAILED"):
        EnergyTools._check_config_hash(config, lock, {})


def test_absent_key_cannot_be_authorized_with_a_fabricated_empty_hash() -> None:
    with pytest.raises(ToolError, match="RESOURCE_LOCKED"):
        EnergyTools._check_config_hash({"new": []}, {"new": compute_config_hash({"new": []})}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "type"])
async def test_config_cannot_override_command_envelope(world, field: str) -> None:
    tool, state = world
    with pytest.raises(ToolError, match="reserved"):
        await tool.ha_manage_energy_prefs(mode="set", config={field: "call_service"}, config_hash="anything")
    assert not state["calls"]


@pytest.mark.asyncio
async def test_missing_component_preview_never_claims_validity(world) -> None:
    tool, state = world
    result = await tool.ha_manage_energy_prefs(mode="set", config={"future_native_field": 123}, dry_run=True)
    assert result["proposal_validation"]["status"] == "unavailable"
    assert result["partial"] is True
    assert result["warnings"]
    assert not saves(state)


@pytest.mark.asyncio
async def test_native_proposal_rejection_prevents_convenience_preview(world, monkeypatch) -> None:
    tool, state = world
    validator = AsyncMock(side_effect=ToolError("VALIDATION_FAILED: native validator rejected payload"))
    monkeypatch.setattr(module, "validate_energy_proposal", validator)
    with pytest.raises(ToolError, match="VALIDATION_FAILED"):
        await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new", dry_run=True)
    proposal = validator.call_args.args[1]
    assert proposal["device_consumption"] == [{"stat_consumption": "sensor.fridge"}, {"stat_consumption": "sensor.new"}]
    assert not saves(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("validation", [
    {"success": False, "error": "offline"},
    {"success": True, "result": {"future_preference": [[{"type": "native_issue", "future_detail": 7}]]}},
])
async def test_post_save_problems_remain_visible_without_reporting_failed_save(world, validation) -> None:
    tool, state = world
    state["validation"] = validation
    result = await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new")
    assert result["success"] is True
    assert result["warnings"]
    assert saves(state)
    if validation["success"]:
        assert "native_issue" in str(result["post_save_validation_errors"])


@pytest.mark.asyncio
async def test_core_save_rejection_is_not_silently_accepted(world) -> None:
    tool, state = world
    state["save_failure"] = "native rejection"
    with pytest.raises(ToolError, match="native rejection"):
        await tool.ha_manage_energy_prefs(mode="add_source", source={"type": "future"})
    assert not any(c["type"] == "energy/validate" for c in state["calls"])


@pytest.mark.asyncio
@pytest.mark.parametrize("water", [False, True])
async def test_device_convenience_roundtrip_preserves_siblings(world, water: bool) -> None:
    tool, state = world
    before = deepcopy(state["prefs"])
    await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new", name="New", included_in_stat="sensor.parent", water=water)
    key = "device_consumption_water" if water else "device_consumption"
    assert state["prefs"][key][-1] == {"stat_consumption": "sensor.new", "name": "New", "included_in_stat": "sensor.parent"}
    await tool.ha_manage_energy_prefs(mode="remove_device", stat_consumption="sensor.new", water=water)
    assert state["prefs"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode,kwargs,code", [
    ("add_device", {"stat_consumption": "sensor.fridge"}, "RESOURCE_ALREADY_EXISTS"),
    ("remove_device", {"stat_consumption": "sensor.absent"}, "RESOURCE_NOT_FOUND"),
    ("add_device", {}, "VALIDATION_MISSING_PARAMETER"),
    ("remove_device", {}, "VALIDATION_MISSING_PARAMETER"),
    ("add_source", {}, "VALIDATION_MISSING_PARAMETER"),
])
async def test_invalid_convenience_operation_never_saves(world, dry_run, mode, kwargs, code) -> None:
    tool, state = world
    with pytest.raises(ToolError, match=code):
        await tool.ha_manage_energy_prefs(mode=mode, dry_run=dry_run, **kwargs)
    assert not saves(state)


@pytest.mark.asyncio
async def test_future_source_fields_reach_core_and_existing_siblings_survive(world) -> None:
    tool, state = world
    state["prefs"]["energy_sources"] = [{"type": "future", "opaque_native_config": {"nested": True}}]
    await tool.ha_manage_energy_prefs(mode="add_source", source={"type": "another_future", "native": [1]})
    assert saves(state)[0]["energy_sources"] == [{"type": "future", "opaque_native_config": {"nested": True}}, {"type": "another_future", "native": [1]}]


@pytest.mark.parametrize("kind,duplicate", [("battery", True), ("future", True), ("grid", False)])
def test_source_duplicate_guard_is_a_wrapper_policy(kind: str, duplicate: bool) -> None:
    source = {"type": kind, "stat_energy_from": "sensor.energy"}
    if duplicate:
        with pytest.raises(ToolError, match="RESOURCE_ALREADY_EXISTS"):
            EnergyTools._append_unique_source([source], source)
    else:
        assert len(EnergyTools._append_unique_source([source], source)) == 2


@pytest.mark.asyncio
async def test_convenience_preview_does_not_write(world) -> None:
    tool, state = world
    before = deepcopy(state["prefs"])
    result = await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new", dry_run=True)
    assert result["new_count"] == 2
    assert result["proposal_validation"]["status"] == "unavailable"
    assert state["prefs"] == before
    assert not saves(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2])
async def test_convenience_retries_conflict_once_with_a_fresh_read(world, monkeypatch, failures: int) -> None:
    tool, state = world
    real_set = tool._set_prefs
    attempts = 0

    async def conflict(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            state["prefs"]["future_preference"] = {"concurrent": attempts}
            raise ToolError('{"error":{"code":"RESOURCE_LOCKED"}}')
        return await real_set(*args, **kwargs)

    monkeypatch.setattr(tool, "_set_prefs", conflict)
    if failures == 2:
        with pytest.raises(ToolError, match="RESOURCE_LOCKED"):
            await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new")
    else:
        await tool.ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.new")
        assert state["prefs"]["future_preference"] == {"concurrent": 1}
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["Command failed: No prefs", "unrelated error"])
async def test_unconfigured_state_is_distinguished_from_read_failure(error: str) -> None:
    client = MagicMock(base_url=None, token=None, send_websocket_message=AsyncMock(return_value={"success": False, "error": error}))
    tool = EnergyTools(client)
    if error.endswith("No prefs"):
        result = await tool.ha_manage_energy_prefs(mode="get")
        assert result["config"] == {}
        assert "unavailable" in result["note"]
    else:
        with pytest.raises(ToolError, match="unrelated error"):
            await tool.ha_manage_energy_prefs(mode="get")


def test_per_key_hashes_have_no_hardcoded_preference_slots() -> None:
    prefs = {"future": {"field": 7}}
    assert _compute_per_key_hashes(prefs) == {"future": compute_config_hash(prefs)}
