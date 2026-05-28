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


def test_load_drops_unknown_fields(tmp_path: Path):
    """``extra='ignore'`` lets policies written by older builds load — fields
    since dropped from the schema (e.g. ``default_action``, ``enabled``)
    must NOT cause ValidationError; they're silently discarded so the next
    save normalises the file."""
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps(
            {
                "wait_seconds": 60,
                "approval_ttl_minutes": 5,
                "rules": [],
                "version": 7,
                "default_action": "deny",  # never-shipped field
                "enabled": True,  # dropped during this PR
            }
        )
    )
    p = load_policy(tmp_path)
    assert p.version == 7
    assert p.rules == []
    assert not hasattr(p, "enabled")  # silently discarded


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


def test_serialized_shape_is_stable(tmp_path: Path):
    save_policy(tmp_path, Policy())
    data = json.loads((tmp_path / POLICY_FILENAME).read_text())
    assert set(data.keys()) == {
        "wait_seconds",
        "approval_ttl_minutes",
        "rules",
        "version",
    }
