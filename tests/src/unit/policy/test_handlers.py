from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.policy.model import Policy, Rule


def make_app(tmp_path: Path, queue: ApprovalQueue) -> TestClient:
    h = build_policy_handlers(data_dir=tmp_path, queue=queue)
    app = Starlette(
        routes=[
            Route("/api/policy/config", h["policy_get_config"], methods=["GET"]),
            Route("/api/policy/config", h["policy_put_config"], methods=["PUT"]),
            Route("/api/policy/pending", h["policy_get_pending"], methods=["GET"]),
            Route("/api/policy/approve", h["policy_post_approve"], methods=["POST"]),
            Route("/api/policy/deny", h["policy_post_deny"], methods=["POST"]),
        ]
    )
    return TestClient(app)


def test_get_config_returns_default(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.get("/api/policy/config")
    assert r.status_code == 200
    assert r.json()["rules"] == []


def test_put_config_roundtrip(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    body = Policy(rules=[Rule(tool_name="ha_x")]).model_dump(mode="json")
    assert c.put("/api/policy/config", json=body).status_code == 200
    rules = c.get("/api/policy/config").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["tool_name"] == "ha_x"


def test_put_config_validation_error_returns_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.put("/api/policy/config", json={"wait_seconds": "not-an-int"})
    assert r.status_code == 400


def test_approve_flow(tmp_path):
    queue = ApprovalQueue()
    entry = queue.create("ha_x", "deadbeef", {"foo": "bar"}, ttl_minutes=5)
    c = make_app(tmp_path, queue)

    assert c.get("/api/policy/pending").json()["pending"][0]["token"] == entry.token

    r = c.post("/api/policy/approve", json={"token": entry.token})
    assert r.status_code == 200
    assert queue.get(entry.token).decision == "approved"


def test_approve_unknown_token_404(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.post("/api/policy/approve", json={"token": "nope"})
    assert r.status_code == 404


def test_deny_flow(tmp_path):
    queue = ApprovalQueue()
    entry = queue.create("ha_x", "deadbeef", {}, ttl_minutes=5)
    c = make_app(tmp_path, queue)
    r = c.post("/api/policy/deny", json={"token": entry.token})
    assert r.status_code == 200
    assert queue.get(entry.token).decision == "denied"


def test_approve_bad_json_body_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.post(
        "/api/policy/approve",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_approve_non_object_body_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.post("/api/policy/approve", json=["just-a-list"])
    assert r.status_code == 400


def test_deny_bad_json_body_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.post(
        "/api/policy/deny",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_deny_non_object_body_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.post("/api/policy/deny", json=["just-a-list"])
    assert r.status_code == 400


def test_approve_already_decided_returns_409(tmp_path):
    """Second approve on the same token → 409 with current_decision.

    Without the 409 the UI can't distinguish "I already clicked this"
    from a generic transport failure, and a racing second approver
    would silently no-op without any signal.
    """
    queue = ApprovalQueue()
    entry = queue.create("ha_x", "deadbeef", {}, ttl_minutes=5)
    c = make_app(tmp_path, queue)
    assert c.post("/api/policy/approve", json={"token": entry.token}).status_code == 200
    r = c.post("/api/policy/approve", json={"token": entry.token})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "already decided"
    assert body["current_decision"] == "approved"


def test_deny_already_decided_returns_409(tmp_path):
    queue = ApprovalQueue()
    entry = queue.create("ha_x", "deadbeef", {}, ttl_minutes=5)
    c = make_app(tmp_path, queue)
    assert c.post("/api/policy/deny", json={"token": entry.token}).status_code == 200
    r = c.post("/api/policy/deny", json={"token": entry.token})
    assert r.status_code == 409
    body = r.json()
    assert body["current_decision"] == "denied"


def test_approve_then_deny_returns_409(tmp_path):
    """Cross-decision (approve then deny) must also 409, not flip the entry."""
    queue = ApprovalQueue()
    entry = queue.create("ha_x", "deadbeef", {}, ttl_minutes=5)
    c = make_app(tmp_path, queue)
    assert c.post("/api/policy/approve", json={"token": entry.token}).status_code == 200
    r = c.post("/api/policy/deny", json={"token": entry.token})
    assert r.status_code == 409
    assert queue.get(entry.token).decision == "approved"


def test_get_config_returns_500_when_policy_corrupt(tmp_path):
    """Corrupt JSON on disk → 500 with ``policy_file_corrupt: true``.

    Lets the UI render a clear repair prompt instead of spinnering
    forever on an opaque server error.
    """
    (tmp_path / "tool_policy.json").write_text("{not valid json")
    c = make_app(tmp_path, ApprovalQueue())
    r = c.get("/api/policy/config")
    assert r.status_code == 500
    body = r.json()
    assert body["policy_file_corrupt"] is True
    assert "error" in body


def test_put_with_stale_version_returns_409(tmp_path):
    """Optimistic concurrency: PUT with stale version → 409.

    save_policy bumps version on every write. A second writer that GET'd
    the policy *before* the first PUT will carry the old version, and
    its PUT must be rejected so the user sees the conflict instead of
    silently clobbering the first writer's edit.
    """
    c = make_app(tmp_path, ApprovalQueue())
    # First write: starts at version=0 → on-disk becomes 1.
    body0 = Policy(rules=[Rule(tool_name="ha_first")]).model_dump(mode="json")
    assert c.put("/api/policy/config", json=body0).status_code == 200
    # Second writer carries the old version=0 instead of re-reading.
    stale = Policy(rules=[Rule(tool_name="ha_second")], version=0).model_dump(
        mode="json"
    )
    r = c.put("/api/policy/config", json=stale)
    assert r.status_code == 409
    payload = r.json()
    assert "policy version mismatch" in payload["error"]
    assert payload["current_version"] == 1
    assert payload["current_policy"]["rules"][0]["tool_name"] == "ha_first"


def test_put_config_clears_remember_cache_when_rules_change(tmp_path):
    """A tightened rule must take effect immediately. Without
    invalidation, an approval remembered before the save would still
    grant pass-through until the remember window expires."""
    queue = ApprovalQueue()
    queue.remember("ha_call_service", "abc123", minutes=10)
    assert queue.is_remembered("ha_call_service", "abc123") is True

    c = make_app(tmp_path, queue)
    current = c.get("/api/policy/config").json()
    body = Policy(
        rules=[Rule(tool_name="ha_new_rule")], version=current["version"]
    ).model_dump(mode="json")
    assert c.put("/api/policy/config", json=body).status_code == 200

    assert queue.is_remembered("ha_call_service", "abc123") is False, (
        "rule change must invalidate the remember-cache"
    )


def test_put_config_preserves_remember_cache_when_only_timing_changes(tmp_path):
    """Editing wait_seconds / approval_ttl_minutes alone shouldn't
    blow away in-flight remembered approvals — only a rule change
    could meaningfully alter outcomes for those calls."""
    queue = ApprovalQueue()
    queue.remember("ha_call_service", "abc123", minutes=10)

    c = make_app(tmp_path, queue)
    current = c.get("/api/policy/config").json()
    # Same rules (empty), different wait_seconds
    body = Policy(wait_seconds=30, version=current["version"]).model_dump(mode="json")
    assert c.put("/api/policy/config", json=body).status_code == 200

    assert queue.is_remembered("ha_call_service", "abc123") is True, (
        "timing-only edit must not invalidate the remember-cache"
    )


def test_get_pending_returns_full_shape(tmp_path):
    queue = ApprovalQueue()
    queue.create("ha_x", "abc", {"foo": "bar"}, ttl_minutes=5)
    c = make_app(tmp_path, queue)
    r = c.get("/api/policy/pending")
    assert r.status_code == 200
    payload = r.json()["pending"][0]
    assert set(payload.keys()) == {
        "token",
        "tool_name",
        "args",
        "created_at",
        "expires_at",
    }
    assert payload["tool_name"] == "ha_x"
    assert payload["args"] == {"foo": "bar"}
    # ISO 8601 with timezone
    assert "T" in payload["created_at"]
    assert "T" in payload["expires_at"]
