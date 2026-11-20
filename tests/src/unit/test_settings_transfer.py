"""Portable embedded-server settings travel between independent data directories."""

import json
import os
from pathlib import Path

import pytest

from ha_mcp.settings_transfer import (
    export_server_settings,
    restore_server_settings,
    rollback_server_settings,
)


def test_transfer_moves_policy_tools_visibility_and_backup_options(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    documents: dict[str, dict[str, object]] = {
        "tool_policy.json": {"rule_effect": "allow", "rules": []},
        "tool_config.json": {"tools": {"ha_call_service": "disabled"}},
        "entity_visibility.json": {"enabled": True, "deny_entity_ids": ["light.guest"]},
        "backup_settings.json": {"auto_backup_retain_per_entity": 12},
        "feature_flags.json": {"redact_secrets": True},
    }
    for name, document in documents.items():
        (source / name).write_text(json.dumps(document))
    (source / "oauth_clients.json").write_text('{"credential":"source-only"}')
    (target / "oauth_clients.json").write_text('{"credential":"target-only"}')

    archive = export_server_settings(source)
    assert "source-only" not in json.dumps(archive)
    restore_server_settings(target, archive)
    for name, expected in documents.items():
        actual = json.loads((target / name).read_text())
        assert {key: actual[key] for key in expected} == expected
    assert json.loads((target / "oauth_clients.json").read_text()) == {
        "credential": "target-only"
    }
    assert restore_server_settings(target, archive) is None
    assert export_server_settings(target) == archive


def test_default_source_resets_previously_configured_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "feature_flags.json").write_text('{"redact_secrets":true}')
    restore_server_settings(tmp_path, export_server_settings(source))
    assert not (tmp_path / "feature_flags.json").exists()


@pytest.mark.parametrize(
    ("section", "filename", "target", "incoming"),
    [
        ("policy", "tool_policy.json", {"wait_seconds": 50}, {"wait_seconds": 20}),
        ("visibility", "entity_visibility.json", {"enabled": True}, {"enforce": True}),
    ],
)
def test_import_advances_the_local_edit_revision_even_on_reset(
    tmp_path, section, filename, target, incoming
):
    path = tmp_path / filename
    path.write_text(json.dumps({**target, "version": 7}))
    before = export_server_settings(tmp_path)
    assert "version" not in before[section]
    bundle = {**before, section: {**incoming, "version": 500}}
    assert restore_server_settings(tmp_path, bundle) is not None
    assert json.loads(path.read_text())["version"] == 8
    assert restore_server_settings(tmp_path, bundle) is None
    assert json.loads(path.read_text())["version"] == 8
    reset = {**bundle, section: None}
    assert restore_server_settings(tmp_path, reset) is not None
    assert json.loads(path.read_text())["version"] == 9
    assert restore_server_settings(tmp_path, reset) is None
    assert export_server_settings(tmp_path)[section] is None
    assert restore_server_settings(tmp_path, before) is not None
    assert json.loads(path.read_text())["version"] == 10
    assert export_server_settings(tmp_path) == before


@pytest.mark.parametrize(
    ("section", "payload"),
    [
        ("../outside", {}),
        ("policy", {"rule_effect": "unknown"}),
        ("tools", {"tools": {"ha_call_service": []}}),
        ("visibility", {"deny_entity_ids": "light.guest"}),
        ("backup", {"homeassistant_token": "unrelated"}),
        ("features", {"ipv6_allowed_networks": "bad"}),
    ],
)
def test_invalid_import_preserves_all_old_settings(tmp_path, section, payload):
    stored = tmp_path / "tool_config.json"
    stored.write_text('{"tools":{"ha_call_service":"disabled"}}')
    archive = export_server_settings(tmp_path)
    archive["tools"] = None
    archive[section] = payload
    with pytest.raises(ValueError):
        restore_server_settings(tmp_path, archive)
    assert json.loads(stored.read_text()) == {"tools": {"ha_call_service": "disabled"}}


def test_write_failure_rolls_back_prior_documents(tmp_path, monkeypatch):
    old_tools = {"tools": {"ha_search": "disabled"}}
    (tmp_path / "tool_config.json").write_text(json.dumps(old_tools))
    original = export_server_settings(tmp_path)
    replacement = {**original, "tools": {}, "policy": {"rule_effect": "allow"}}
    replace = os.replace

    def fail_policy_write(source, destination):
        if Path(destination).name == "tool_policy.json":
            raise OSError("storage unavailable")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_policy_write)
    with pytest.raises(OSError):
        restore_server_settings(tmp_path, replacement)
    assert export_server_settings(tmp_path) == original


def test_undo_failure_does_not_skip_earlier_files_or_mask_the_import_error(
    tmp_path, monkeypatch, caplog
):
    policy = tmp_path / "tool_policy.json"
    policy.write_text('{"wait_seconds":20}')
    original = export_server_settings(tmp_path)
    imported = {
        **original,
        "tools": {"tools": {}},
        "policy": {"wait_seconds": 30},
        "visibility": {"enabled": True},
    }
    write_error = OSError("private write details")
    replace = os.replace

    def fail_two_writes(src, dest):
        if Path(dest).name == "entity_visibility.json":
            raise write_error
        if (
            Path(dest) == policy
            and json.loads(Path(src).read_text())["wait_seconds"] == 20
        ):
            raise OSError("private rollback details")
        return replace(src, dest)

    monkeypatch.setattr(os, "replace", fail_two_writes)
    with pytest.raises(OSError) as raised:
        restore_server_settings(tmp_path, imported)
    assert raised.value is write_error
    assert not (tmp_path / "tool_config.json").exists()
    assert json.loads(policy.read_text())["wait_seconds"] == 30
    assert "rollback failed for tool_policy.json (OSError)" in caplog.text
    assert "private" not in caplog.text


def test_later_rollback_only_reverts_unchanged_import_writes(tmp_path):
    original = export_server_settings(tmp_path)
    changes = restore_server_settings(
        tmp_path,
        {
            **original,
            "policy": {"wait_seconds": 20},
            "visibility": {"enabled": True},
            "tools": {"tools": {"ha_search": "disabled"}},
            "features": {"redact_secrets": True},
        },
    )
    assert changes is not None
    # A subsequent policy save now has identical portable content but a new
    # edit revision. A save to an unversioned file can also rewrite identical JSON.
    policy = tmp_path / "tool_policy.json"
    saved_policy = {**json.loads(policy.read_text()), "version": 12}
    policy.write_text(json.dumps(saved_policy))
    features = tmp_path / "feature_flags.json"
    saved_features = tmp_path / "separate-save.json"
    saved_features.write_bytes(features.read_bytes())
    os.replace(saved_features, features)
    backup = tmp_path / "backup_settings.json"
    backup.write_text('{"enable_auto_backup":false}')

    rollback_server_settings(tmp_path, changes)
    assert json.loads(policy.read_text()) == saved_policy
    assert json.loads(features.read_text()) == {"redact_secrets": True}
    assert json.loads(backup.read_text()) == {"enable_auto_backup": False}
    assert not (tmp_path / "tool_config.json").exists()
    visibility = json.loads((tmp_path / "entity_visibility.json").read_text())
    assert visibility["enabled"] is False
    assert visibility["version"] == 3


@pytest.mark.parametrize(
    ("before", "after", "restart"),
    [
        (None, {"individual_fetch_batch_size": 15}, True),
        ({"individual_fetch_batch_size": 15}, None, True),
        (
            {"individual_fetch_batch_size": 15},
            {"individual_fetch_batch_size": 20},
            True,
        ),
        (
            {"individual_fetch_batch_size": 15},
            {"individual_fetch_batch_size": 15, "read_only_mode": True},
            False,
        ),
    ],
)
def test_receipt_flags_changed_restart_overrides(tmp_path, before, after, restart):
    if before is not None:
        (tmp_path / "feature_flags.json").write_text(json.dumps(before))
    original = export_server_settings(tmp_path)
    changes = restore_server_settings(tmp_path, {**original, "features": after})
    assert changes is not None
    assert changes.restart_required is restart


@pytest.mark.parametrize(
    ("effect", "expected_rules"), [("require_approval", 2), ("allow", 1)]
)
def test_unstamped_policy_keeps_its_startup_semantics_on_transfer(
    tmp_path, effect, expected_rules
):
    legacy = {
        "rule_effect": effect,
        "rules": [
            {
                "tool_name": "ha_call_service",
                "when": [
                    {"path": "args.domain", "op": "eq", "value": "switch"},
                    {"path": "args.service", "op": "eq", "value": "turn_on"},
                ],
            }
        ],
    }
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool_policy.json").write_text(json.dumps(legacy))
    archive = export_server_settings(source)
    assert len(archive["policy"]["rules"]) == expected_rules
    assert archive["policy"]["schema_version"] == 2
    assert restore_server_settings(tmp_path, archive) is not None
    assert restore_server_settings(tmp_path, archive) is None
    assert export_server_settings(tmp_path) == archive
