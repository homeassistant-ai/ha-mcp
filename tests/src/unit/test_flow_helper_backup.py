"""Pre-write backups of flow helpers other than template (#2632).

Every flow helper (a config entry) ha_config_set_helper edits is snapshotted
as ``helper_<type>`` from its stored options, restored through its options
flow, and recreated through its creation flow when deleted. What a form does
not offer comes from Home Assistant's own forms, not a per-type list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.tools.config_entry_flow import CreationFlowError, create_flow_helper

_METER = {
    "name": "Energy",
    "source": "sensor.power",
    "cycle": "daily",
    "periodically_resetting": True,
}


def _listing(options: dict[str, Any], helper_type: str = "utility_meter") -> dict:
    return {
        "covered_types": [helper_type],
        "helpers": [
            {
                "kind": "flow",
                "helper_type": helper_type,
                "entry_id": "meter-entry",
                "entity_id": "sensor.energy",
                "options": options,
            }
        ],
    }


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The meter's options as the component reports them; tests change it."""
    current = dict(_METER)

    async def send(client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "ha_mcp_tools/helpers_list":
            return _listing(dict(current))
        if message["type"] == "config/entity_registry/list":
            return []
        raise AssertionError(message)

    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=send))
    return current


def _options_flow(offered: list[str]) -> SimpleNamespace:
    """utility_meter's options form offers source and periodically_resetting."""
    return SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "utility_meter"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "meter-options",
                "step_id": "init",
                "data_schema": [{"name": name} for name in offered],
                "last_step": True,
            }
        ),
        submit_options_flow_step=AsyncMock(
            return_value={"type": "create_entry", "result": {}}
        ),
        abort_options_flow=AsyncMock(),
    )


async def test_capture_reads_the_stored_options(stored: dict[str, Any]) -> None:
    handler = bm._make_flow_helper_handler("utility_meter")
    snapshot = await handler.fetch(None, "sensor.energy")
    assert snapshot["entry_id"] == "meter-entry"
    assert snapshot["options"] == _METER


async def test_restore_submits_what_the_options_form_offers(
    stored: dict[str, Any],
) -> None:
    client = _options_flow(["source", "periodically_resetting"])
    stored["source"] = "sensor.other"

    async def applied(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        stored.update(payload)  # HA saves the submitted options
        return {"type": "create_entry", "result": {}}

    client.submit_options_flow_step.side_effect = applied
    handler = bm._make_flow_helper_handler("utility_meter")
    await handler.restore(
        client, "meter-entry", {"entry_id": "meter-entry", "options": dict(_METER)}
    )
    client.submit_options_flow_step.assert_awaited_once_with(
        "meter-options", {"source": "sensor.power", "periodically_resetting": True}
    )


async def test_unmarked_last_form_is_applied_and_judged_by_readback(
    stored: dict[str, Any],
) -> None:
    """A flow that never marks its last form (``last_step`` None) cannot be
    checked before it applies: the forms are submitted, and a snapshot option
    no form offers that differs is reported by the readback, not refused."""
    client = _options_flow(["source", "periodically_resetting"])
    client.start_options_flow.return_value["last_step"] = None
    stored["cycle"] = "monthly"

    async def applied(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        stored.update(payload)
        return {"type": "create_entry", "result": {}}

    client.submit_options_flow_step.side_effect = applied
    handler = bm._make_flow_helper_handler("utility_meter")
    with pytest.raises(bm.BackupRestoreError) as caught:
        await handler.restore(
            client, "meter-entry", {"entry_id": "meter-entry", "options": dict(_METER)}
        )
    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"
    client.submit_options_flow_step.assert_awaited_once()
    assert stored["cycle"] == "monthly"


async def test_restore_refuses_a_changed_option_the_form_cannot_set(
    stored: dict[str, Any],
) -> None:
    """A meter's cycle is fixed at creation: restoring an older cycle is refused
    with nothing applied, rather than reported as done."""
    client = _options_flow(["source", "periodically_resetting"])
    stored["cycle"] = "monthly"
    handler = bm._make_flow_helper_handler("utility_meter")
    with pytest.raises(bm.BackupRestoreError, match="no restore form offers") as caught:
        await handler.restore(
            client, "meter-entry", {"entry_id": "meter-entry", "options": dict(_METER)}
        )
    assert caught.value.outcome["apply_status"] == "not_applied"
    client.submit_options_flow_step.assert_not_awaited()


def _creation_client(*replies: dict[str, Any], first: dict[str, Any]) -> Any:
    return SimpleNamespace(
        start_config_flow=AsyncMock(return_value=first),
        submit_config_flow_step=AsyncMock(side_effect=list(replies)),
        abort_config_flow=AsyncMock(),
    )


_CREATED = {"type": "create_entry", "result": {"entry_id": "new-entry"}}


async def test_edit_whose_capture_the_component_cannot_serve_warns_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The write goes ahead, and the response (not only the log) says that no
    backup was taken and why."""
    from ha_mcp.tools import auto_backup

    async def send(client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "ha_mcp_tools/helpers_list":
            return {"covered_types": [], "helpers": []}  # meters not covered
        raise AssertionError(message)

    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=send))
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: settings)
    client = SimpleNamespace()

    @auto_backup.with_auto_backup(
        domain="helper_utility_meter", id_param="helper_id", client=client
    )
    async def edit(helper_id: str) -> dict[str, Any]:
        return {"success": True}

    result = await edit(helper_id="meter-entry")

    assert result["success"] is True
    (warning,) = result["warnings"]
    assert warning.startswith("No pre-write backup of helper_utility_meter:meter-entry")
    assert "cannot authoritatively read utility_meter" in warning


async def test_every_flow_helper_edit_runs_in_the_restore_critical_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard that keeps an edit and a restore of one entry apart is keyed
    by domain family, not by the template type alone."""
    from ha_mcp.tools import auto_backup

    entered: list[str] = []

    @asynccontextmanager
    async def guard(entry_id: str) -> AsyncIterator[None]:
        entered.append(entry_id)
        yield

    manager = SimpleNamespace(config_entry_write_guard=guard)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda c, s: manager)
    async with auto_backup._template_write_context(
        object(), SimpleNamespace(), "helper_utility_meter", "meter-entry"
    ):
        pass
    async with auto_backup._template_write_context(
        object(), SimpleNamespace(), "helper_utility_meter", "sensor.alias"
    ):
        pass  # an alias target resolves on its own path

    assert entered == ["meter-entry"]


async def test_recreation_answers_the_menu_from_the_snapshot() -> None:
    """A group stores its branch as group_type; the creation menu's option
    with that value is chosen, and no form is asked for the key."""
    client = _creation_client(
        {
            "type": "form",
            "flow_id": "create",
            "step_id": "light",
            "data_schema": [{"name": "name"}, {"name": "entities"}],
            "last_step": True,
        },
        _CREATED,
        first={
            "type": "menu",
            "flow_id": "create",
            "menu_options": ["binary_sensor", "light"],
        },
    )
    options = {"group_type": "light", "name": "Hall", "entities": ["light.a"]}
    result = await create_flow_helper(client, "group", options, complete_snapshot=True)
    assert result["entry_id"] == "new-entry"
    assert [c.args[1] for c in client.submit_config_flow_step.await_args_list] == [
        {"next_step_id": "light"},
        {"name": "Hall", "entities": ["light.a"]},
    ]


async def test_recreation_picks_the_branch_key_over_a_matching_option_value() -> None:
    """A template binary_sensor with device_class "light" holds two values that
    name template branches; template_type is the branch, not device_class."""
    client = _creation_client(
        {
            "type": "form",
            "flow_id": "create",
            "step_id": "binary_sensor",
            "data_schema": [{"name": n} for n in ("name", "state", "device_class")],
            "last_step": True,
        },
        _CREATED,
        first={
            "type": "menu",
            "flow_id": "create",
            "menu_options": ["binary_sensor", "light", "sensor"],
        },
    )
    options = {
        "template_type": "binary_sensor",
        "device_class": "light",
        "name": "Hall light",
        "state": "{{ 1 }}",
    }
    result = await create_flow_helper(
        client, "template", options, complete_snapshot=True
    )
    assert result["entry_id"] == "new-entry"
    assert [c.args[1] for c in client.submit_config_flow_step.await_args_list] == [
        {"next_step_id": "binary_sensor"},
        {"device_class": "light", "name": "Hall light", "state": "{{ 1 }}"},
    ]
    assert "warnings" not in result


async def test_recreation_fills_every_form_of_a_multi_step_flow() -> None:
    """statistics asks for its characteristic on a second form."""
    form = {"type": "form", "flow_id": "create", "last_step": False}
    client = _creation_client(
        {
            **form,
            "step_id": "state_characteristic",
            "data_schema": [{"name": "state_characteristic"}],
        },
        {**form, "step_id": "options", "data_schema": [{"name": "sampling_size"}]},
        _CREATED,
        first={
            **form,
            "step_id": "user",
            "data_schema": [{"name": "name"}, {"name": "entity_id"}],
        },
    )
    options = {
        "name": "Mean",
        "entity_id": "sensor.t",
        "state_characteristic": "mean",
        "sampling_size": 20,
    }
    result = await create_flow_helper(
        client, "statistics", options, complete_snapshot=True
    )
    assert result["entry_id"] == "new-entry"
    assert [c.args[1] for c in client.submit_config_flow_step.await_args_list] == [
        {"name": "Mean", "entity_id": "sensor.t"},
        {"state_characteristic": "mean"},
        {"sampling_size": 20},
    ]


async def test_entities_renamed_before_a_later_one_fails_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A utility meter with tariffs recreates several entities; when the second
    cannot be found, the outcome still names the first's rename."""
    row = {"entity_id": "sensor.energy_2", "unique_id": "new-entry", "name": None}

    async def created(client: Any, entry_id: str, unique_id: str) -> dict[str, Any]:
        if unique_id == "new-entry_peak":
            raise bm.HomeAssistantError("Recreated entity mapping is ambiguous")
        return dict(row)

    async def rename(client: Any, message: dict[str, Any]) -> None:
        row["entity_id"] = message["new_entity_id"]

    monkeypatch.setattr(bm, "_created_entity", created)
    monkeypatch.setattr(bm, "_check_entity_collision", AsyncMock())
    monkeypatch.setattr(bm, "_ws_send", rename)
    saved = [
        {"entity_id": "sensor.energy", "unique_id": "old-entry", "name": None},
        {
            "entity_id": "sensor.energy_peak",
            "unique_id": "old-entry_peak",
            "name": None,
        },
    ]

    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._restore_entity_ids(None, "new-entry", "old-entry", saved)

    assert caught.value.outcome["entity_id_mapping"] == [
        {"created_entity_id": "sensor.energy_2", "restored_entity_id": "sensor.energy"}
    ]
    assert caught.value.outcome["verification_status"] == "unavailable"


async def test_recreation_refuses_a_snapshot_key_no_form_takes() -> None:
    client = _creation_client(
        _CREATED,
        first={
            "type": "form",
            "flow_id": "create",
            "step_id": "user",
            "data_schema": [{"name": "name"}],
            "last_step": True,
        },
    )
    with pytest.raises(CreationFlowError) as caught:
        await create_flow_helper(
            client, "derivative", {"name": "D", "extra": 1}, complete_snapshot=True
        )
    assert caught.value.reason == "unsupported_fields"
    assert caught.value.apply_status == "not_applied"
    client.submit_config_flow_step.assert_not_awaited()
