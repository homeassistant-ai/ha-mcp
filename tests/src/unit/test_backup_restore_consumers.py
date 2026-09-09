"""Restore outcomes survive both consumers and the production Settings script."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp import backup_manager as bm
from ha_mcp.backup_manager import BackupRestoreError, MandatoryBackupError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.settings_ui import _handlers_backups as ui

from ._js_harness import extract_script_body, run_script
from .test_backup_diff_error_mapping import _dispatcher
from .test_settings_ui_js_behavior import DEFAULT_FETCHES, MIN_DOM, _assert_clean_init

NAME = "helper_template.sensor.example.20260909_000000.yaml"
SAFETY = "helper_template.original-entry.20260909_000001.yaml"


@pytest.mark.parametrize("consumer", ["settings", "mcp"])
@pytest.mark.parametrize(
    ("apply_status", "verification_status"),
    [
        ("not_applied", "not_run"),
        ("unknown", "unavailable"),
        ("applied", "mismatched"),
        ("applied", "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_typed_restore_outcome_survives_consumer(
    monkeypatch, consumer, apply_status, verification_status
):
    error = BackupRestoreError(
        "Restore could not be completed.",
        apply_status=apply_status,
        verification_status=verification_status,
        restored_from=NAME,
        domain="helper_template",
        entity_id="original-entry",
        safety_backup=SAFETY,
    )
    manager = SimpleNamespace(restore_snapshot=AsyncMock(side_effect=error))
    if consumer == "settings":
        response = response_for(monkeypatch, manager)
        assert response.headers["content-type"].startswith("application/json")
        assert not response.is_success
        payload = response.json()
    else:
        monkeypatch.setattr(
            "ha_mcp.tools.backup.get_backup_manager", lambda *args: manager
        )
        monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", SimpleNamespace)
        with pytest.raises(ToolError) as caught:
            await _dispatcher()(scope="edits", action="restore", backup_name=NAME)
        payload = json.loads(str(caught.value))
    assert payload["success"] is False
    assert payload["data"] == error.outcome
    assert payload["error"]["message"] == str(error)
    assert SAFETY in str(payload)


def response_for(monkeypatch, manager, name=NAME):
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
    handler = ui.build_backups_handlers(None)["restore_backup"]
    app = Starlette(
        routes=[Route("/backups/{name}/restore", handler, methods=["POST"])]
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(f"/backups/{name}/restore")


def test_settings_mandatory_capture_failure_is_json_and_does_not_echo_details(
    monkeypatch,
):
    manager = SimpleNamespace(
        restore_snapshot=AsyncMock(
            side_effect=MandatoryBackupError(
                "transport error with secret-marker",
                suggestions=["Check backup storage"],
            )
        )
    )
    response = response_for(monkeypatch, manager)
    assert response.headers["content-type"].startswith("application/json")
    assert not response.is_success
    payload = response.json()
    assert payload["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert payload["data"]["apply_status"] == "not_applied"
    assert "Nothing was changed" in payload["error"]["message"]
    assert "secret-marker" not in response.text


@pytest.mark.asyncio
async def test_settings_diff_uses_stable_snapshot_comparison(monkeypatch):
    snapshot = {
        "domain": "helper_template",
        "entity_id": "original-entry",
        "config": {"state": "saved"},
    }
    manager = SimpleNamespace(
        snapshot_comparison=AsyncMock(
            return_value=(snapshot, {"state": "original live"})
        ),
        read_snapshot=Mock(return_value={**snapshot, "entity_id": "sensor.reused"}),
        handler_for=Mock(
            return_value=SimpleNamespace(
                fetch=AsyncMock(return_value={"state": "replacement live"})
            )
        ),
    )
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
    response = await ui._diff_backup(
        None, Request({"type": "http", "path_params": {"name": NAME}})
    )
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert "current:original-entry" in payload["diff"]
    assert "+state: original live" in payload["diff"]
    assert "replacement live" not in payload["diff"]
    manager.snapshot_comparison.assert_awaited_once_with(NAME)
    manager.read_snapshot.assert_not_called()


@pytest.fixture(scope="module")
def settings_script():
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)


def run_restore(settings_script, reply, name=NAME):
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        settle_ms=300,
        fetch_map={
            **DEFAULT_FETCHES,
            "/restore": reply,
            "/backups?": {"status": 200, "json": {"success": True, "backups": []}},
        },
        invoke=(
            "await new Promise(resolve => setTimeout(resolve, 100));\n"
            f"await window.backupAction('restore', {json.dumps(name)});"
        ),
    )
    _assert_clean_init(result)
    return result


@pytest.mark.parametrize(
    ("apply_status", "verification_status", "expected"),
    [
        ("not_applied", "not_run", "Nothing was changed"),
        ("unknown", "unavailable", "could not be confirmed"),
        ("applied", "unavailable", "verification is unavailable"),
        ("applied", "mismatched", "does not match"),
    ],
)
def test_settings_script_preserves_failure_outcome_and_refreshes_inventory(
    settings_script, apply_status, verification_status, expected
):
    result = run_restore(
        settings_script,
        {
            "status": 409,
            "json": {
                "success": False,
                "error": {"message": "Restore could not be completed."},
                "data": {
                    "restored_from": NAME,
                    "safety_backup": SAFETY,
                    "apply_status": apply_status,
                    "verification_status": verification_status,
                },
            },
        },
    )
    assert len(result.alerts) == 1
    assert expected in result.alerts[0]
    assert SAFETY in result.alerts[0]
    assert "restore" in result.alerts[0].lower()
    assert len(result.fetches_to("/backups?")) == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"status": 500, "body": "Internal Server Error"},
        {"throw": "connection lost after POST"},
    ],
)
def test_settings_script_uncertain_response_avoids_blind_retry(settings_script, reply):
    result = run_restore(settings_script, reply)
    assert "could not be confirmed" in result.dom
    assert "before retrying" in result.dom
    assert len(result.fetches_to("/backups?")) == 1


def test_settings_script_success_false_is_not_reported_restored(settings_script):
    result = run_restore(
        settings_script,
        {
            "status": 200,
            "json": {
                "success": False,
                "error": {"message": "Restore refused"},
                "data": {
                    "apply_status": "not_applied",
                    "verification_status": "not_run",
                },
            },
        },
    )
    assert "Nothing was changed" in result.alerts[0]
    assert "Restored." not in result.alerts[0]


@pytest.fixture
def template_runtime(tmp_path, monkeypatch):
    """Real manager and options walker with synthetic HA transport responses."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state = SimpleNamespace(
        options={"name": "Example", "template_type": "sensor", "state": "{{ 12 }}"},
        committed=False,
        mode="success",
        restore_reads=0,
        alias="sensor.example",
        replacement=False,
    )

    async def submit(flow_id, payload):
        assert flow_id == "offline-options"
        state.options = {"name": "Example", "template_type": "sensor", **payload}
        state.committed = True
        if state.mode == "lost_submit_reply":
            raise HomeAssistantConnectionError("secret-marker lost submit response")
        return {"type": "create_entry", "result": {}}

    client = SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "offline-options",
                "step_id": "sensor",
                "data_schema": [
                    {"name": "state", "required": True, "selector": {"template": {}}}
                ],
            }
        ),
        submit_options_flow_step=AsyncMock(side_effect=submit),
        abort_options_flow=AsyncMock(),
    )

    async def send(_client, command):
        assert command["type"] == "ha_mcp_tools/helpers_list"
        if state.mode == "safety_failure":
            state.restore_reads += 1
            if state.restore_reads == 2:
                raise HomeAssistantConnectionError("secret-marker capture unavailable")
        if state.committed and state.mode != "success":
            raise HomeAssistantConnectionError("secret-marker readback unavailable")
        helpers = [
            {
                "kind": "flow",
                "helper_type": "template",
                "entry_id": "original-entry",
                "entity_id": state.alias,
                "options": deepcopy(state.options),
            }
        ]
        if state.replacement:
            helpers.append(
                {
                    "kind": "flow",
                    "helper_type": "template",
                    "entry_id": "replacement-entry",
                    "entity_id": "sensor.example",
                    "options": {
                        "name": "Replacement",
                        "template_type": "sensor",
                        "state": "{{ 999 }}",
                    },
                }
            )
        return {"covered_types": ["template"], "helpers": helpers}

    monkeypatch.setattr(bm, "_ws_send", send)
    manager = bm.BackupManager(
        SimpleNamespace(
            auto_backup_dir=str(tmp_path / "backups"),
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
        ),
        client,
    )
    bm.register_default_handlers(manager, client)
    monkeypatch.setattr(ui, "_backup_mgr", lambda server: manager)
    return SimpleNamespace(manager=manager, state=state, client=client)


@pytest.mark.parametrize(
    ("mode", "apply_status"),
    [
        ("safety_failure", "not_applied"),
        ("lost_verify_reply", "applied"),
        ("lost_submit_reply", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_manager_http_and_production_js_preserve_restore_outcome(
    template_runtime, settings_script, monkeypatch, mode, apply_status
):
    runtime = template_runtime
    snapshot = await runtime.manager.maybe_snapshot(
        "helper_template", "sensor.example", force=True
    )
    assert snapshot is not None
    runtime.state.options["state"] = "{{ 99 }}"
    runtime.state.mode = mode
    response = response_for(monkeypatch, runtime.manager, snapshot.name)
    assert response.headers["content-type"].startswith("application/json")
    assert not response.is_success
    payload = response.json()
    assert payload["data"]["apply_status"] == apply_status
    assert "secret-marker" not in response.text
    rendered = run_restore(
        settings_script,
        {"status": response.status_code, "json": payload},
        snapshot.name,
    )
    if apply_status == "not_applied":
        assert "Nothing was changed" in rendered.alerts[0]
        assert not runtime.state.committed
        assert runtime.state.restore_reads == 2
        runtime.client.start_options_flow.assert_not_awaited()
    else:
        safety = payload["data"]["safety_backup"]
        assert safety in {item["name"] for item in runtime.manager.list_snapshots()}
        assert safety in rendered.alerts[0]
        assert len(rendered.fetches_to("/backups?")) == 1
        assert runtime.state.options["state"] == "{{ 12 }}"


@pytest.mark.asyncio
async def test_manager_settings_preview_follows_original_entry_after_alias_reuse(
    template_runtime,
):
    runtime = template_runtime
    snapshot = await runtime.manager.maybe_snapshot(
        "helper_template", "sensor.example", force=True
    )
    assert snapshot is not None
    runtime.state.options["state"] = "{{ 99 }}"
    runtime.state.alias = "sensor.renamed"
    runtime.state.replacement = True
    response = await ui._diff_backup(
        None, Request({"type": "http", "path_params": {"name": snapshot.name}})
    )
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert "current:original-entry" in payload["diff"]
    assert "99" in payload["diff"]
    assert "999" not in payload["diff"]
