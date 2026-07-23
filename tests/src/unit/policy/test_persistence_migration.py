"""Tests for the one-time ANY-match policy schema migration (PR #1993).

Pre-#1993 policy files pack every UI condition into one rule with the
predicates AND-ed. ``migrate_policy_any_semantics`` splits those rules
(one rule per predicate = one rule per old UI condition) exactly once,
stamping ``schema_version`` so hand-authored multi-predicate rules created
AFTER the upgrade (a condition with AND-ed sub-parameters) are never split.
"""

import json

from ha_mcp.policy.model import POLICY_SCHEMA_VERSION, Policy, Predicate, Rule
from ha_mcp.policy.persistence import (
    POLICY_FILENAME,
    load_policy,
    migrate_policy_any_semantics,
    save_policy,
)


def _write_raw(tmp_path, payload: dict) -> None:
    (tmp_path / POLICY_FILENAME).write_text(json.dumps(payload))


def _read_raw(tmp_path) -> dict:
    return json.loads((tmp_path / POLICY_FILENAME).read_text())


class TestMigration:
    def test_unstamped_multi_predicate_rule_is_split(self, tmp_path):
        _write_raw(
            tmp_path,
            {
                "wait_seconds": 30,
                "approval_ttl_minutes": 5,
                "version": 4,
                "rules": [
                    {
                        "tool_name": "ha_call_service",
                        "when": [
                            {"path": "args.domain", "op": "eq", "value": "lock"},
                            {"path": "args.service", "op": "eq", "value": "unlock"},
                            {"path": "args.entity_id", "op": "exists"},
                        ],
                        "remember_minutes": 7,
                    },
                    {"tool_name": "ha_restart", "when": [], "remember_minutes": 0},
                ],
            },
        )
        assert migrate_policy_any_semantics(tmp_path) is True
        migrated = load_policy(tmp_path)
        # 3 predicates -> 3 single-predicate rules; the bare rule untouched.
        service_rules = [r for r in migrated.rules if r.tool_name == "ha_call_service"]
        assert len(service_rules) == 3
        assert all(len(r.when) == 1 for r in service_rules)
        assert all(r.remember_minutes == 7 for r in service_rules)
        assert [r.tool_name for r in migrated.rules].count("ha_restart") == 1
        # Stamped + version bumped by the save.
        assert migrated.schema_version == POLICY_SCHEMA_VERSION
        assert migrated.version == 5
        # Globals preserved.
        assert migrated.wait_seconds == 30

    def test_unstamped_single_predicate_file_is_stamped_without_split(self, tmp_path):
        _write_raw(
            tmp_path,
            {
                "rules": [
                    {
                        "tool_name": "ha_call_service",
                        "when": [{"path": "args.domain", "op": "eq", "value": "lock"}],
                        "remember_minutes": 0,
                    }
                ],
                "version": 0,
            },
        )
        assert migrate_policy_any_semantics(tmp_path) is True
        migrated = load_policy(tmp_path)
        assert len(migrated.rules) == 1
        assert len(migrated.rules[0].when) == 1
        assert _read_raw(tmp_path)["schema_version"] == POLICY_SCHEMA_VERSION

    def test_stamped_multi_predicate_rule_is_preserved(self, tmp_path):
        # Post-upgrade: a condition with AND-ed sub-parameters must survive
        # restarts un-split.
        save_policy(
            tmp_path,
            Policy(
                rules=[
                    Rule(
                        tool_name="ha_call_service",
                        when=[
                            Predicate(path="args.domain", op="eq", value="lock"),
                            Predicate(path="args.service", op="eq", value="unlock"),
                        ],
                    )
                ]
            ),
        )
        assert _read_raw(tmp_path)["schema_version"] == POLICY_SCHEMA_VERSION
        assert migrate_policy_any_semantics(tmp_path) is False
        kept = load_policy(tmp_path)
        assert len(kept.rules) == 1
        assert len(kept.rules[0].when) == 2

    def test_migration_runs_once(self, tmp_path):
        _write_raw(
            tmp_path,
            {
                "rules": [
                    {
                        "tool_name": "t",
                        "when": [
                            {"path": "a", "op": "exists"},
                            {"path": "b", "op": "exists"},
                        ],
                        "remember_minutes": 0,
                    }
                ],
                "version": 0,
            },
        )
        assert migrate_policy_any_semantics(tmp_path) is True
        assert migrate_policy_any_semantics(tmp_path) is False  # stamped now

    def test_missing_file_is_noop(self, tmp_path):
        assert migrate_policy_any_semantics(tmp_path) is False
        assert not (tmp_path / POLICY_FILENAME).exists()

    def test_corrupt_file_left_alone(self, tmp_path):
        (tmp_path / POLICY_FILENAME).write_text("not json {{{")
        assert migrate_policy_any_semantics(tmp_path) is False
        assert (tmp_path / POLICY_FILENAME).read_text() == "not json {{{"

    def test_invalid_schema_left_alone(self, tmp_path):
        _write_raw(tmp_path, {"rules": [{"tool_name": ""}], "version": 0})
        assert migrate_policy_any_semantics(tmp_path) is False
