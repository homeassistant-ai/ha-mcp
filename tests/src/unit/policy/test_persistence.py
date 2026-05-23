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
        enabled=True,
        rules=[
            Rule(
                tool_name="ha_call_service",
                when=[Predicate(path="args.domain", op="eq", value="lock")],
            )
        ],
    )
    save_policy(tmp_path, original)
    loaded = load_policy(tmp_path)
    # save_policy bumps version on write (optimistic concurrency contract);
    # compare every other field, then version separately.
    assert loaded.version == original.version + 1
    assert loaded.model_copy(update={"version": 0}) == original


def test_save_writes_atomically(tmp_path: Path):
    save_policy(tmp_path, Policy(enabled=True))
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
        "enabled",
        "wait_seconds",
        "approval_ttl_minutes",
        "rules",
        "version",
    }
