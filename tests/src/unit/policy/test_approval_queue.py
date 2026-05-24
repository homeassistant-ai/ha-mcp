from datetime import UTC, datetime, timedelta

import anyio
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
    assert q.approve(p.token) is True
    assert p.decision == "approved"
    assert p._event.is_set()


def test_deny_sets_decision_and_fires_event():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    assert q.deny(p.token) is True
    assert p.decision == "denied"
    assert p._event.is_set()


def test_approve_unknown_token_returns_false():
    q = ApprovalQueue()
    assert q.approve("nope") is False


def test_deny_unknown_token_returns_false():
    q = ApprovalQueue()
    assert q.deny("nope") is False


def test_approve_already_decided_returns_false():
    """Idempotent retries and double-clicks land on the same entry; the
    second caller MUST observe the no-op so the handler can 409 cleanly
    instead of silently re-firing event.set()."""
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    assert q.approve(p.token) is True
    assert q.approve(p.token) is False
    # decision unchanged
    assert p.decision == "approved"


def test_deny_already_decided_returns_false():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    assert q.deny(p.token) is True
    assert q.deny(p.token) is False
    assert p.decision == "denied"


def test_approve_then_deny_returns_false():
    q = ApprovalQueue()
    p = q.create("ha_x", "abc", {}, ttl_minutes=5)
    assert q.approve(p.token) is True
    assert q.deny(p.token) is False
    assert p.decision == "approved"


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

    elapsed: float = 0.0
    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        start = anyio.current_time()
        decision = await p.wait()
        elapsed = anyio.current_time() - start
    assert decision == "approved"
    assert p.decision == "approved"
    # Event-driven wake should land within ~50ms of the approver firing
    # (plus scheduler jitter). A polling impl with a 1s tick would
    # easily exceed 200ms here, so this asserts the event path actually
    # runs and isn't a hidden poll loop.
    assert elapsed < 0.2, f"wait() took {elapsed:.3f}s; expected event wake"


# --- find_or_create + concurrency ---


@pytest.mark.anyio
async def test_find_or_create_returns_existing_when_args_hash_matches():
    q = ApprovalQueue()
    first = q.create("ha_x", "abc", {"a": 1}, ttl_minutes=5)
    second = await q.find_or_create("ha_x", "abc", {"a": 1}, ttl_minutes=5)
    assert second.token == first.token, "should reuse existing pending entry"


@pytest.mark.anyio
async def test_find_or_create_concurrent_callers_share_entry():
    """Two coroutines hitting find_or_create with identical
    (tool_name, args_hash) must end up with the SAME token. The lock
    inside find_or_create is what prevents two duplicate pending
    entries that would each block their own waiter independently."""
    q = ApprovalQueue()
    tokens: list[str] = []

    async def race(idx: int) -> None:
        entry = await q.find_or_create("ha_x", "abc", {}, ttl_minutes=5)
        tokens.append(entry.token)

    async with anyio.create_task_group() as tg:
        for i in range(10):
            tg.start_soon(race, i)

    assert len(tokens) == 10
    assert len(set(tokens)) == 1, f"expected one shared token, got {set(tokens)}"
    assert len(q.list_pending()) == 1


# --- pending-cap enforcement ---


def test_create_evicts_oldest_when_pending_cap_hit():
    q = ApprovalQueue()
    q.PENDING_CAP = 3  # shrink for the test
    a = q.create("ha_x", "h1", {}, ttl_minutes=5)
    b = q.create("ha_x", "h2", {}, ttl_minutes=5)
    c = q.create("ha_x", "h3", {}, ttl_minutes=5)
    d = q.create("ha_x", "h4", {}, ttl_minutes=5)
    # a is oldest → evicted; b/c/d survive.
    assert q.get(a.token) is None
    for entry in (b, c, d):
        assert q.get(entry.token) is not None
