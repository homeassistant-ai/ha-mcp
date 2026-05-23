from datetime import UTC, datetime, timedelta

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
    q._remember[("ha_x", "deadbeef")] = datetime.now(UTC) - timedelta(seconds=1)
    assert q.is_remembered("ha_x", "deadbeef") is False


# --- appended for Task 2.2: pending-entry lifecycle ---
import anyio


def test_create_returns_pending_entry():
    q = ApprovalQueue()
    p = q.create("ha_call_service", "abc", {"domain": "lock"}, ttl_minutes=5)
    assert p.decision == "pending"
    assert p.tool_name == "ha_call_service"
    assert p.args_hash == "abc"
    assert q.find("ha_call_service", "abc") is p


def test_find_returns_none_for_unknown():
    q = ApprovalQueue()
    assert q.find("ha_x", "abc") is None


def test_approve_sets_decision_and_fires_event():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    q.approve(p.token)
    assert p.decision == "approved"
    assert p.event.is_set()


def test_deny_sets_decision_and_fires_event():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    q.deny(p.token)
    assert p.decision == "denied"
    assert p.event.is_set()


def test_remove_deletes_entry():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    q.remove(p.token)
    assert q.find("ha_x", "abc") is None


def test_expired_pending_treated_as_missing():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    p.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert q.find("ha_x", "abc") is None  # auto-expired


def test_list_pending_returns_only_unresolved():
    q = ApprovalQueue()
    p1 = q.create("ha_x", "abc", {}, ttl_minutes=5)
    p2 = q.create("ha_y", "def", {}, ttl_minutes=5)
    q.approve(p2.token)
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0].token == p1.token


def test_consume_and_maybe_remember_no_remember():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    q.approve(p.token)
    q.consume_and_maybe_remember(p, remember_minutes=0)
    # single-shot: entry removed, no remember-cache entry
    assert q.find("ha_x", "abc") is None
    assert q.is_remembered("ha_x", "abc") is False


def test_consume_and_maybe_remember_with_remember():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    q.approve(p.token)
    q.consume_and_maybe_remember(p, remember_minutes=10)
    assert q.find("ha_x", "abc") is None
    assert q.is_remembered("ha_x", "abc") is True


@pytest.mark.anyio
async def test_event_wakes_waiter():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)

    async def approver():
        await anyio.sleep(0.05)
        q.approve(p.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        await p.event.wait()
    assert p.decision == "approved"
