"""Portable embedded-server settings travel between independent data directories."""

import json
import os
from pathlib import Path

import pytest

from ha_mcp.settings_transfer import export_server_settings, restore_server_settings


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
    assert restore_server_settings(tmp_path, bundle) == before
    assert json.loads(path.read_text())["version"] == 8
    assert restore_server_settings(tmp_path, bundle) is None
    assert json.loads(path.read_text())["version"] == 8
    restored = export_server_settings(tmp_path)
    reset = {**bundle, section: None}
    assert restore_server_settings(tmp_path, reset) == restored
    assert json.loads(path.read_text())["version"] == 9
    assert restore_server_settings(tmp_path, reset) is None
    assert export_server_settings(tmp_path)[section] is None
    assert restore_server_settings(tmp_path, before) == reset
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
