"""Generic config-entry mutations retain recoverable Template options."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp import backup_manager as bm
from ha_mcp.tools import auto_backup
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.fixture
def entry_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    entry = {
        "entry_id": "template-entry",
        "domain": "template",
        "title": "Example",
        "disabled_by": None,
    }
    options = {"name": "Example", "template_type": "sensor", "state": "{{ 12 }}"}
    entity = {
        "entity_id": "sensor.example",
        "unique_id": "template-entry",
        "name": "Renamed Example",
        "original_name": "Example",
    }
    client = SimpleNamespace(
        get_config_entry=AsyncMock(side_effect=lambda _: deepcopy(entry)),
        abort_options_flow=AsyncMock(),
    )
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    manager = bm.get_backup_manager(client, settings)
    mutations = []

    async def snapshot_read(_client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "config_entries/get":
            return [deepcopy(entry)]
        if message["type"] == "config/entity_registry/list":
            return [{**entity, "config_entry_id": entry["entry_id"]}]
        assert message["type"] == "ha_mcp_tools/helpers_list"
        return {
            "covered_types": ["template"],
            "helpers": [
                {
                    "kind": "flow",
                    "helper_type": "template",
                    "entry_id": entry["entry_id"],
                    "entity_id": entity["entity_id"],
                    "options": deepcopy(options),
                }
            ],
        }

    async def options_flow(_entry_id: str) -> dict[str, Any]:
        return {
            "type": "form",
            "flow_id": "edit-flow",
            "step_id": options["template_type"],
            "data_schema": [
                {"name": "state", "required": True, "selector": {"template": {}}}
            ],
        }

    def observe_mutation() -> None:
        snapshots = list(manager.backup_dir.glob("*.yaml"))
        mutations.append(
            {
                "snapshots": [manager.read_snapshot(path.name) for path in snapshots],
                "locked": manager._entry_write_locks[entry["entry_id"]].locked(),
            }
        )

    async def submit_options(_flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        observe_mutation()
        options.update(data)
        return {"type": "create_entry", "result": {}}

    async def delete_entry(_entry_id: str) -> dict[str, Any]:
        observe_mutation()
        return {"require_restart": False}

    async def set_enabled(message: dict[str, Any]) -> dict[str, Any]:
        assert message["type"] == "config_entries/disable"
        observe_mutation()
        entry["disabled_by"] = message["disabled_by"]
        return {"success": True, "result": {"require_restart": False}}

    client.start_options_flow = AsyncMock(side_effect=options_flow)
    client.submit_options_flow_step = AsyncMock(side_effect=submit_options)
    client.delete_config_entry = AsyncMock(side_effect=delete_entry)
    client.send_websocket_message = AsyncMock(side_effect=set_enabled)
    monkeypatch.setattr(bm, "_ws_send", snapshot_read)
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: settings)
    return SimpleNamespace(
        entry=entry,
        entity=entity,
        options=options,
        manager=manager,
        client=client,
        tools=IntegrationTools(client),
        mutations=mutations,
    )


@pytest.mark.parametrize("operation", ["options", "delete"])
@pytest.mark.parametrize("template_type", ["sensor", "binary_sensor"])
async def test_generic_template_mutation_captures_options_before_write(
    entry_backup: SimpleNamespace, operation: str, template_type: str
) -> None:
    entry_backup.options["template_type"] = template_type
    entry_backup.entity["entity_id"] = f"{template_type}.example"
    original = deepcopy(entry_backup.options)
    if operation == "options":
        result = await entry_backup.tools.ha_set_integration(
            entry_id="template-entry", config={"state": "{{ 99 }}"}
        )
        tool_name = "ha_set_integration"
    else:
        result = await entry_backup.tools.ha_remove_helpers_integrations(
            target="template-entry", confirm=True
        )
        tool_name = "ha_remove_helpers_integrations"

    assert result["success"] is True
    assert len(entry_backup.mutations) == 1
    mutation = entry_backup.mutations[0]
    assert mutation["locked"] is True
    assert len(mutation["snapshots"]) == 1
    snapshot = mutation["snapshots"][0]
    assert snapshot["domain"] == "helper_template"
    assert snapshot["entity_id"] == "template-entry"
    assert snapshot["tool"] == tool_name
    assert snapshot["config"] == {
        "entry_id": "template-entry",
        "options": original,
        "entities": [entry_backup.entity],
    }


async def test_explicit_template_entry_id_is_rejected_without_capture(
    entry_backup: SimpleNamespace,
) -> None:
    with pytest.raises(ToolError, match="ENTITY_NOT_FOUND"):
        await entry_backup.tools.ha_remove_helpers_integrations(
            target="template-entry", helper_type="template", confirm=True, wait=False
        )

    entry_backup.client.delete_config_entry.assert_not_awaited()
    assert entry_backup.mutations == []
    assert not list(entry_backup.manager.backup_dir.glob("*.yaml"))


async def test_explicit_template_entity_id_captures_once_after_resolution(
    entry_backup: SimpleNamespace,
) -> None:
    async def registry_read(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "config/entity_registry/get":
            assert message["entity_id"] == "sensor.example"
            return {"success": True, "result": {"config_entry_id": "template-entry"}}
        assert message["type"] == "config/entity_registry/list"
        return {"success": True, "result": []}

    entry_backup.client.send_websocket_message.side_effect = registry_read
    result = await entry_backup.tools.ha_remove_helpers_integrations(
        target="sensor.example", helper_type="template", confirm=True, wait=False
    )

    assert result["success"] is True
    entry_backup.client.delete_config_entry.assert_awaited_once_with("template-entry")
    assert len(entry_backup.mutations) == 1
    mutation = entry_backup.mutations[0]
    assert mutation["locked"] is True
    assert len(mutation["snapshots"]) == 1
    snapshot = mutation["snapshots"][0]
    assert snapshot["domain"] == "helper_template"
    assert snapshot["entity_id"] == "template-entry"
    assert snapshot["config"]["options"] == entry_backup.options


@pytest.mark.parametrize("enabled", [False, True])
async def test_template_enable_disable_keeps_integration_backup(
    entry_backup: SimpleNamespace, enabled: bool
) -> None:
    entry_backup.entry["disabled_by"] = "user" if enabled else None
    original = deepcopy(entry_backup.entry)

    result = await entry_backup.tools.ha_set_integration(
        entry_id="template-entry", enabled=enabled
    )

    assert result["success"] is True
    assert entry_backup.entry["disabled_by"] == (None if enabled else "user")
    mutation = entry_backup.mutations[0]
    assert mutation["locked"] is True
    assert len(mutation["snapshots"]) == 1
    snapshot = mutation["snapshots"][0]
    assert snapshot["domain"] == "integration"
    assert snapshot["config"] == original
    entry_backup.client.get_config_entry.assert_not_awaited()


@pytest.mark.parametrize("operation", ["options", "delete"])
async def test_other_integration_mutations_keep_existing_backup(
    entry_backup: SimpleNamespace, operation: str
) -> None:
    entry_backup.entry["domain"] = "workday"
    original = deepcopy(entry_backup.entry)

    if operation == "options":
        result = await entry_backup.tools.ha_set_integration(
            entry_id="template-entry", config={"state": "{{ 99 }}"}
        )
    else:
        result = await entry_backup.tools.ha_remove_helpers_integrations(
            target="template-entry", confirm=True
        )

    assert result["success"] is True
    mutation = entry_backup.mutations[0]
    assert mutation["locked"] is True
    assert len(mutation["snapshots"]) == 1
    snapshot = mutation["snapshots"][0]
    assert snapshot["domain"] == "integration"
    assert snapshot["config"] == original


@pytest.mark.parametrize("operation", ["options", "delete"])
@pytest.mark.parametrize("capture_state", ["disabled", "lookup_failed"])
async def test_generic_template_mutation_preserves_best_effort_capture_policy(
    entry_backup: SimpleNamespace, operation: str, capture_state: str
) -> None:
    if capture_state == "disabled":
        entry_backup.manager._settings.enable_auto_backup = False
    else:
        entry_backup.client.get_config_entry.side_effect = [
            bm.HomeAssistantError("entry lookup temporarily unavailable"),
            deepcopy(entry_backup.entry),
        ]

    if operation == "options":
        result = await entry_backup.tools.ha_set_integration(
            entry_id="template-entry", config={"state": "{{ 99 }}"}
        )
    else:
        result = await entry_backup.tools.ha_remove_helpers_integrations(
            target="template-entry", confirm=True
        )

    assert result["success"] is True
    assert entry_backup.mutations == [{"snapshots": [], "locked": True}]
    # The options walker itself reads the entry once, independently of backup.
    assert entry_backup.client.get_config_entry.await_count == (
        int(operation == "options") + int(capture_state == "lookup_failed")
    )
