import json
from pathlib import Path

import pytest

from ha_mcp.policy.model import Policy, Predicate, Rule
from ha_mcp.policy.persistence import POLICY_FILENAME, load_policy, save_policy


def test_load_missing_file_returns_default(tmp_path: Path):
    p = load_policy(tmp_path)
    assert p == Policy()


def test_save_and_roundtrip(tmp_path: Path):
    original = Policy(
        wait_seconds=30,
        approval_ttl_minutes=10,
        rules=[
            Rule(
                tool_name="ha_call_service",
                when=[Predicate(path="args.domain", op="eq", value="lock")],
                remember_minutes=5,
            )
        ],
    )
    save_policy(tmp_path, original)
    loaded = load_policy(tmp_path)
    # save_policy bumps version on write (optimistic concurrency contract);
    # compare every other field, then version separately.
    assert loaded.version == original.version + 1
    assert loaded.model_copy(update={"version": 0}) == original
    # Every authored field must survive the roundtrip — earlier this test
    # silently passed with a non-existent `enabled=True` field that
    # Policy.extra="ignore" dropped on construction, hiding the loss.
    assert loaded.wait_seconds == 30
    assert loaded.approval_ttl_minutes == 10
    assert loaded.rules[0].remember_minutes == 5


def test_load_refuses_unknown_fields(tmp_path: Path):
    """An unknown key fails the load instead of being dropped: a dropped
    misspelling such as "rule_efect": "allow" would read an allow list as a
    require-approval list (issue #2540). The middleware turns the ValueError
    into a fail-closed POLICY_LOAD_FAILED."""
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps({"rule_efect": "allow", "rules": [], "version": 7})
    )
    with pytest.raises(ValueError, match="rule_efect"):
        load_policy(tmp_path)


def test_save_writes_atomically(tmp_path: Path):
    save_policy(tmp_path, Policy())
    assert (tmp_path / POLICY_FILENAME).exists()
    # tmpfile should not survive
    tmp_files = list(tmp_path.glob(f".{POLICY_FILENAME}.*"))
    assert tmp_files == []


def test_corrupt_file_raises(tmp_path: Path):
    (tmp_path / POLICY_FILENAME).write_text("{not json")
    with pytest.raises(ValueError):
        load_policy(tmp_path)


def test_a_file_that_is_not_utf8_is_named_in_the_error(tmp_path: Path):
    (tmp_path / POLICY_FILENAME).write_bytes(b"\xff\xfe{}")
    with pytest.raises(ValueError, match=r"tool_policy\.json is not valid JSON"):
        load_policy(tmp_path)


def test_serialized_shape_is_stable(tmp_path: Path):
    save_policy(tmp_path, Policy())
    data = json.loads((tmp_path / POLICY_FILENAME).read_text())
    assert set(data.keys()) == {
        "wait_seconds",
        "approval_ttl_minutes",
        "rules",
        "version",
        # ANY-match schema marker (PR #1993) — see migrate_policy_any_semantics.
        "schema_version",
        # Deciding a held request from the event bus (issue #2502). The
        # switch is part of the document; the PIN it depends on is not --
        # that lives in approval_pin.json, out of reach of every reader of
        # this file.
        "event_decisions_enabled",
        # Whether a matching rule gates or approves the call (issue #2540).
        "rule_effect",
    }
