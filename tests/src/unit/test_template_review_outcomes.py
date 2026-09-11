"""Review regressions for Template snapshot validation and recovery diagnostics."""

import logging
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import yaml

from ha_mcp import backup_manager as bm
from ha_mcp.tools.config_entry_flow import OptionsFlowError

from .test_template_backup_review import manager as manager
from .test_template_deleted_recovery import ENTITY, OPTIONS
from .test_template_deleted_recovery import recovery as recovery


@pytest.mark.parametrize("field", ["domain", "entity_id", "config"])
async def test_diff_rejects_missing_snapshot_envelope_field(manager, field):
    fetch = AsyncMock(return_value={})
    manager.register(bm.DomainHandler("automation", fetch, AsyncMock()))
    path = manager._write_snapshot("automation", "example", {}, "test")
    snapshot = manager.read_snapshot(path.name)
    snapshot.pop(field)
    path.write_text(yaml.safe_dump(snapshot))
    with pytest.raises(ValueError, match="Snapshot.*missing.*" + field):
        await manager.snapshot_comparison(path.name)
    fetch.assert_not_awaited()


@pytest.mark.parametrize("field", ["domain", "entity_id"])
async def test_diff_rejects_non_string_snapshot_identity(manager, field):
    fetch = AsyncMock(return_value={})
    manager.register(bm.DomainHandler("automation", fetch, AsyncMock()))
    path = manager._write_snapshot("automation", "example", {}, "test")
    snapshot = manager.read_snapshot(path.name)
    snapshot[field] = None
    path.write_text(yaml.safe_dump(snapshot))
    with pytest.raises(ValueError, match="Snapshot.*invalid.*" + field):
        await manager.snapshot_comparison(path.name)
    fetch.assert_not_awaited()


@pytest.mark.parametrize("config", [{"entry_id": "old-entry"}, {"options": OPTIONS}])
async def test_diff_rejects_invalid_snapshot_before_live_fetch(manager, config):
    path = manager._write_snapshot("helper_template", "old-entry", config, "test")
    fetch = AsyncMock(return_value={"entry_id": "old-entry", "options": OPTIONS})
    manager.register(bm.DomainHandler("helper_template", fetch, AsyncMock()))
    with pytest.raises(ValueError, match="Template helper snapshot") as caught:
        await manager.snapshot_comparison(path.name)
    assert type(caught.value).__name__ == "InvalidBackupSnapshotError"
    fetch.assert_not_awaited()


@pytest.mark.parametrize("options", [None, {}, {"template_type": "sensor"}])
async def test_malformed_snapshot_is_permanent_refusal_before_live_read(
    recovery, options, caplog
):
    recovery.config["options"] = options
    path = recovery.manager._write_snapshot(
        "helper_template", "old-entry", recovery.config, "test"
    )
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await recovery.manager.restore_snapshot(path.name)
    assert caught.value.outcome["reason"] == "invalid_snapshot"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["verification_status"] == "not_run"
    recovery.create.assert_not_awaited()
    recovery.manager._client._request.assert_not_awaited()
    assert "snapshot_validation" in caplog.text


async def test_recreated_identity_readback_mismatch_is_reported(recovery, monkeypatch):
    send = bm._ws_send.side_effect

    async def ignore_rename(client, message):
        if message["type"] == "config/entity_registry/update":
            return {}  # HA acknowledges without the requested identity taking effect.
        return await send(client, message)

    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=ignore_rename))
    with pytest.raises(bm.BackupRestoreError) as caught:
        await recovery.manager.restore_snapshot(recovery.name)
    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"
    assert caught.value.outcome["reason"] == "entity_identity_mismatch"
    assert caught.value.outcome["entity_id"] == "new-entry"


@pytest.mark.parametrize("entities", [{}, [None], [ENTITY, ENTITY]])
async def test_invalid_entity_metadata_is_a_snapshot_refusal(recovery, entities):
    recovery.config["entities"] = entities
    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._recreate_template_helper(
            recovery.manager._client, "old-entry", recovery.config
        )
    assert caught.value.outcome["reason"] == "invalid_snapshot"
    recovery.manager._client._request.assert_not_awaited()
    recovery.create.assert_not_awaited()


async def test_recreated_identity_read_failure_is_unavailable(recovery, monkeypatch):
    monkeypatch.setattr(
        bm,
        "_created_template_entity",
        AsyncMock(side_effect=bm.HomeAssistantError("private upstream payload")),
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await recovery.manager.restore_snapshot(recovery.name)
    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "unavailable"


async def test_applied_options_refusal_preserves_safe_details(monkeypatch):
    from ha_mcp.tools import config_entry_flow

    snapshot = {"entry_id": "old-entry", "options": OPTIONS}
    monkeypatch.setattr(bm, "_fetch_template_helper", AsyncMock(return_value=snapshot))
    failure = OptionsFlowError(
        "private upstream payload",
        apply_status="applied",
        entry_id="old-entry",
        reason="unsupported_fields",
        fields=("availability",),
    )
    monkeypatch.setattr(
        config_entry_flow, "update_config_entry_options", AsyncMock(side_effect=failure)
    )
    with pytest.raises(bm.BackupRestoreError) as caught:
        await bm._restore_template_helper(object(), "old-entry", snapshot)
    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "matched"
    assert caught.value.outcome["reason"] == "unsupported_fields"
    assert caught.value.outcome["fields"] == ["availability"]
    assert "private upstream payload" not in str(caught.value)


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"secret_scrub_degraded": True}, "secret_scrub_degraded"),
        ({"covered_types": []}, "template_read_unsupported"),
        (
            {
                "covered_types": ["template"],
                "helpers": [{"kind": "flow", "helper_type": "template"}],
            },
            "ambiguous_entry_identity",
        ),
    ],
)
async def test_unavailable_verification_logs_safe_local_cause(
    monkeypatch, caplog, response, reason
):
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(return_value=response))
    with caplog.at_level(logging.WARNING):
        result = await bm._verify_template_restore(object(), "old-entry", {})
    assert result == "unavailable"
    assert "options_readback" in caplog.text
    assert reason in caplog.text


async def test_unknown_recreation_logs_step_and_type_without_remote_payload(
    recovery, caplog
):
    recovery.create.side_effect = bm.HomeAssistantError("private upstream payload")
    with caplog.at_level(logging.WARNING), pytest.raises(bm.BackupRestoreError):
        await recovery.manager.restore_snapshot(recovery.name)
    assert "create_entry" in caplog.text
    assert "HomeAssistantError" in caplog.text
    assert "private upstream payload" not in caplog.text


def test_redaction_marker_inside_a_template_remains_a_recoverable_value():
    options = {**OPTIONS, "state": '{{ "**redacted**" }}'}
    assert bm._template_options(options) == options


@pytest.mark.parametrize(
    "value", ["**redacted**", {"nested": "**redacted**"}, ["**redacted**"]]
)
def test_exact_redaction_leaves_are_still_refused(value):
    with pytest.raises(bm.HomeAssistantError, match="redacted"):
        bm._template_options({**OPTIONS, "state": value})


@pytest.mark.parametrize(
    "registry",
    [None, [None], [{}], [{"entity_id": "no_domain"}], [ENTITY, ENTITY]],
)
async def test_malformed_registry_refuses_capture_without_writing(
    recovery, monkeypatch, registry
):
    recovery.state.records = [
        {
            "entry_id": "old-entry",
            "kind": "flow",
            "helper_type": "template",
            "options": deepcopy(OPTIONS),
        }
    ]
    recovery.state.registry = registry
    before = set(recovery.manager.backup_dir.glob("*.yaml"))
    with pytest.raises(bm.MandatoryBackupError):
        await recovery.manager.maybe_snapshot(
            "helper_template", "old-entry", mandatory=True, force=True
        )
    assert set(recovery.manager.backup_dir.glob("*.yaml")) == before
