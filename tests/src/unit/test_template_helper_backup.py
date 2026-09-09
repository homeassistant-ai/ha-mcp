"""Template-helper backups preserve options, never entity-state surrogates."""

import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantError


def _record(options=None):
    return {
        "kind": "flow",
        "helper_type": "template",
        "entry_id": "template-entry",
        "entity_id": "sensor.renamed_example",
        "options": options
        if options is not None
        else {"name": "Example", "template_type": "sensor", "state": "{{ 12 }}"},
    }


def _response(record=None):
    return {"covered_types": ["template"], "helpers": [record or _record()]}


@pytest.fixture
def manager(tmp_path):
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )
    return bm.get_backup_manager(SimpleNamespace(), settings)


@pytest.mark.parametrize("target", ["template-entry", "sensor.renamed_example"])
async def test_capture_complete_options_without_opening_flow(
    manager, monkeypatch, target
):
    send = AsyncMock(return_value=_response())
    monkeypatch.setattr(bm, "_ws_send", send)
    name = await manager.maybe_snapshot(
        "helper_template", target, tool_name="ha_config_set_helper"
    )
    assert name is not None
    snapshot = manager.read_snapshot(Path(name).name)
    assert snapshot["config"] == {
        "entry_id": "template-entry",
        "options": _record()["options"],
    }
    send.assert_awaited_once_with(
        manager._client,
        {
            "type": "ha_mcp_tools/helpers_list",
            "helper_types": ["template"],
            "include_flow_helpers": True,
        },
    )


@pytest.mark.parametrize(
    "response",
    [
        {"helpers": [_record()]},
        {"covered_types": [], "helpers": []},
        {"covered_types": ["template"], "helpers": None},
        {"covered_types": "template", "helpers": [_record()]},
        {"covered_types": ["template"], "helpers": [None]},
        _response(_record({"state": "{{ 12 }}"})),
        _response(
            _record(
                {"name": "Example", "template_type": "sensor", "state": "<redacted>"}
            )
        ),
        _response(
            _record(
                {
                    "name": "Example",
                    "template_type": "sensor",
                    "additional_options": {"availability": "**redacted**"},
                }
            )
        ),
    ],
)
async def test_incomplete_or_redacted_capture_is_not_a_backup(
    manager, monkeypatch, response
):
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=response))
    handler = manager.handler_for("helper_template")
    assert handler is not None
    with pytest.raises(HomeAssistantError):
        await handler.fetch(manager._client, "template-entry")


async def test_absent_helper_does_not_capture_state_stub(manager, monkeypatch):
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(return_value={"covered_types": ["template"], "helpers": []}),
    )
    handler = manager.handler_for("helper_template")
    assert handler is not None
    assert await handler.fetch(manager._client, "missing") is None


async def test_restore_submits_snapshot_and_clears_later_optional_values(
    manager, monkeypatch
):
    original = _record()["options"]
    current = {**original, "state": "{{ 99 }}", "unit_of_measurement": "W"}
    send = AsyncMock(
        side_effect=[_response(_record(current)), _response(_record(original))]
    )
    monkeypatch.setattr(bm, "_ws_send", send)
    client = SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "restore-flow",
                "step_id": "sensor",
                "data_schema": [
                    {
                        "name": "state",
                        "required": True,
                        "selector": {"template": {}},
                        "description": {"suggested_value": "{{ 99 }}"},
                    },
                    {
                        "name": "unit_of_measurement",
                        "required": False,
                        "selector": {"text": {}},
                        "description": {"suggested_value": "W"},
                    },
                ],
            }
        ),
        submit_options_flow_step=AsyncMock(
            return_value={"type": "create_entry", "result": {}}
        ),
        abort_options_flow=AsyncMock(),
    )
    handler = manager.handler_for("helper_template")
    assert handler is not None
    result = await handler.restore(
        client,
        "template-entry",
        {"entry_id": "template-entry", "options": deepcopy(original)},
    )
    assert result["success"] is True
    client.submit_options_flow_step.assert_awaited_once_with(
        "restore-flow", {"state": "{{ 12 }}"}
    )
    client.abort_options_flow.assert_not_awaited()


@pytest.mark.parametrize(
    "changed", [{"template_type": "binary_sensor"}, {"name": "Another"}]
)
async def test_restore_refuses_changed_identity_before_starting_flow(
    manager, monkeypatch, changed
):
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(return_value=_response(_record({**_record()["options"], **changed}))),
    )
    client = SimpleNamespace(start_options_flow=AsyncMock())
    handler = manager.handler_for("helper_template")
    assert handler is not None
    with pytest.raises(HomeAssistantError):
        await handler.restore(
            client,
            "template-entry",
            {"entry_id": "template-entry", "options": _record()["options"]},
        )
    client.start_options_flow.assert_not_awaited()


async def test_restore_does_not_report_success_on_readback_mismatch(
    manager, monkeypatch
):
    from ha_mcp.tools import config_entry_flow

    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            return_value=_response(
                _record({**_record()["options"], "state": "{{ 99 }}"})
            )
        ),
    )
    monkeypatch.setattr(
        config_entry_flow,
        "update_config_entry_options",
        AsyncMock(return_value={"success": True}),
    )
    handler = manager.handler_for("helper_template")
    assert handler is not None
    with pytest.raises(HomeAssistantError, match=r"applied.*verification"):
        await handler.restore(
            manager._client,
            "template-entry",
            {"entry_id": "template-entry", "options": _record()["options"]},
        )


async def test_readback_transport_failure_reports_applied_but_unverified(
    manager, monkeypatch
):
    from ha_mcp.tools import config_entry_flow

    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(side_effect=[_response(), HomeAssistantError("offline")]),
    )
    monkeypatch.setattr(
        config_entry_flow,
        "update_config_entry_options",
        AsyncMock(return_value={"success": True}),
    )
    handler = manager.handler_for("helper_template")
    assert handler is not None
    with pytest.raises(
        HomeAssistantError, match=r"applied.*verification is unavailable"
    ):
        await handler.restore(
            manager._client,
            "template-entry",
            {"entry_id": "template-entry", "options": _record()["options"]},
        )


async def test_restore_clears_nested_optional_section(manager, monkeypatch):
    from ha_mcp.tools.config_entry_flow import update_config_entry_options

    client = SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "restore-flow",
                "step_id": "sensor",
                "data_schema": [
                    {"name": "state", "required": True, "selector": {"template": {}}},
                    {
                        "name": "additional_options",
                        "type": "expandable",
                        "required": False,
                        "schema": [
                            {
                                "name": "availability",
                                "required": False,
                                "selector": {"template": {}},
                                "description": {"suggested_value": "{{ false }}"},
                            },
                        ],
                    },
                ],
            }
        ),
        submit_options_flow_step=AsyncMock(
            return_value={"type": "create_entry", "result": {}}
        ),
        abort_options_flow=AsyncMock(),
    )
    result = await update_config_entry_options(
        client,
        "template-entry",
        {"state": "{{ 12 }}"},
        expected_domain="template",
        keep_current_values=False,
    )
    assert result["success"] is True
    client.submit_options_flow_step.assert_awaited_once_with(
        "restore-flow", {"state": "{{ 12 }}"}
    )


@pytest.mark.parametrize(
    "current", [None, {"entry_id": "template-entry", "options": _record()["options"]}]
)
async def test_deleted_entry_and_target_mismatch_never_start_restore(
    manager, monkeypatch, current
):
    monkeypatch.setattr(bm, "_fetch_template_helper", AsyncMock(return_value=current))
    client = SimpleNamespace(start_options_flow=AsyncMock())
    handler = manager.handler_for("helper_template")
    assert handler is not None
    target = "template-entry" if current is None else "different-entry"
    with pytest.raises(HomeAssistantError):
        await handler.restore(
            client,
            target,
            {"entry_id": "template-entry", "options": _record()["options"]},
        )
    client.start_options_flow.assert_not_awaited()


async def _captured_alias(manager, monkeypatch):
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=_response()))
    path = await manager.maybe_snapshot("helper_template", "sensor.renamed_example")
    assert path is not None
    return path.name


async def test_manager_safety_capture_follows_entry_after_rename_and_bypasses_toggle_throttle(
    manager, monkeypatch
):
    name = await _captured_alias(manager, monkeypatch)
    current = _record({**_record()["options"], "state": "{{ 99 }}"})
    current["entity_id"] = "sensor.now_renamed"
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=_response(current)))
    manager._settings.enable_auto_backup = False
    manager._settings.auto_backup_throttle_minutes = 30
    manager._last_snapshot["helper_template:template-entry"] = time.monotonic()
    restore = AsyncMock(return_value={"success": True})
    manager.register(
        bm.DomainHandler("helper_template", bm._fetch_template_helper, restore)
    )
    result = await manager.restore_snapshot(name)
    assert result["entity_id"] == "template-entry"
    assert result["safety_backup"] is not None
    safety = manager.read_snapshot(result["safety_backup"])
    assert safety["entity_id"] == "template-entry"
    assert safety["config"]["options"] == current["options"]
    assert restore.await_args.args[1] == "template-entry"


async def test_manager_safety_capture_failure_blocks_restore(manager, monkeypatch):
    name = await _captured_alias(manager, monkeypatch)
    monkeypatch.setattr(
        bm, "_ws_send", AsyncMock(side_effect=HomeAssistantError("offline"))
    )
    restore = AsyncMock()
    manager.register(
        bm.DomainHandler("helper_template", bm._fetch_template_helper, restore)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(name)
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    restore.assert_not_awaited()


async def test_manager_preserves_safety_filename_on_restore_error(manager, monkeypatch):
    name = await _captured_alias(manager, monkeypatch)
    restore = AsyncMock(
        side_effect=HomeAssistantError("applied but verification unavailable")
    )
    manager.register(
        bm.DomainHandler("helper_template", bm._fetch_template_helper, restore)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(name)
    safety_name = caught.value.outcome["safety_backup"]
    assert caught.value.outcome["apply_status"] == "unknown"
    assert manager.read_snapshot(safety_name)["entity_id"] == "template-entry"


async def test_manager_diff_uses_captured_entry_not_reused_entity_alias(
    manager, monkeypatch
):
    name = await _captured_alias(manager, monkeypatch)
    original = _record()
    original["entity_id"] = "sensor.now_renamed"
    replacement = _record({**_record()["options"], "state": "{{ 999 }}"})
    replacement["entry_id"] = "replacement-entry"
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            return_value={
                "covered_types": ["template"],
                "helpers": [replacement, original],
            }
        ),
    )
    diff = await manager.diff_snapshot(name)
    assert diff["unchanged"] is True


async def test_button_restore_clears_press_without_submitting_creation_only_device_class(
    manager, monkeypatch
):
    original = {"name": "Example", "template_type": "button", "device_class": "restart"}
    current = {
        **original,
        "press": [
            {"action": "light.turn_on", "target": {"entity_id": "light.example"}}
        ],
    }
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            side_effect=[_response(_record(current)), _response(_record(original))]
        ),
    )
    client = SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "button-restore",
                "step_id": "button",
                "data_schema": [
                    {
                        "name": "press",
                        "required": False,
                        "selector": {"action": {}},
                        "description": {"suggested_value": current["press"]},
                    },
                ],
            }
        ),
        submit_options_flow_step=AsyncMock(
            return_value={"type": "create_entry", "result": {}}
        ),
        abort_options_flow=AsyncMock(),
    )
    handler = manager.handler_for("helper_template")
    result = await handler.restore(
        client, "template-entry", {"entry_id": "template-entry", "options": original}
    )
    assert result["success"] is True
    client.submit_options_flow_step.assert_awaited_once_with("button-restore", {})


@pytest.mark.parametrize("template_type", ["button", "cover", "event", "update"])
async def test_changed_creation_only_device_class_refused_before_apply(
    manager, monkeypatch, template_type
):
    original = {
        "name": "Example",
        "template_type": template_type,
        "device_class": "original-class",
    }
    monkeypatch.setattr(
        bm,
        "_ws_send",
        AsyncMock(
            return_value=_response(
                _record({**original, "device_class": "changed-class"})
            )
        ),
    )
    client = SimpleNamespace(start_options_flow=AsyncMock())
    handler = manager.handler_for("helper_template")
    with pytest.raises(HomeAssistantError, match="identity changed"):
        await handler.restore(
            client,
            "template-entry",
            {"entry_id": "template-entry", "options": original},
        )
    client.start_options_flow.assert_not_awaited()
