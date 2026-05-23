from datetime import datetime, timedelta, timezone

import pytest

from ha_mcp.policy.approval_queue import (
    ApprovalQueue,
    compute_args_hash,
)


def test_args_hash_stable_across_key_order():
    a = compute_args_hash({"domain": "lock", "service": "unlock"})
    b = compute_args_hash({"service": "unlock", "domain": "lock"})
    assert a == b


def test_args_hash_changes_on_value_change():
    a = compute_args_hash({"domain": "lock"})
    b = compute_args_hash({"domain": "light"})
    assert a != b


def test_args_hash_nested():
    a = compute_args_hash({"config": {"alias": "x"}})
    b = compute_args_hash({"config": {"alias": "y"}})
    assert a != b


def test_remember_cache_miss():
    q = ApprovalQueue()
    assert q.is_remembered("ha_x", "deadbeef") is False


def test_remember_cache_hit_within_ttl():
    q = ApprovalQueue()
    q.remember("ha_x", "deadbeef", minutes=5)
    assert q.is_remembered("ha_x", "deadbeef") is True


def test_remember_cache_expired():
    q = ApprovalQueue()
    q.remember("ha_x", "deadbeef", minutes=5)
    # rewind expiry into the past
    q._remember[("ha_x", "deadbeef")] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert q.is_remembered("ha_x", "deadbeef") is False
