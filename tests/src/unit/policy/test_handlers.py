from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.policy.model import Policy, Rule


def make_app(tmp_path: Path, queue: ApprovalQueue) -> TestClient:
    h = build_policy_handlers(data_dir=tmp_path, queue=queue)
    app = Starlette(routes=[
        Route("/api/policy/config", h["policy_get_config"], methods=["GET"]),
        Route("/api/policy/config", h["policy_put_config"], methods=["PUT"]),
        Route("/api/policy/pending", h["policy_get_pending"], methods=["GET"]),
        Route("/api/policy/approve", h["policy_post_approve"], methods=["POST"]),
        Route("/api/policy/deny", h["policy_post_deny"], methods=["POST"]),
    ])
    return TestClient(app)


def test_get_config_returns_default(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.get("/api/policy/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_put_config_roundtrip(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    body = Policy(enabled=True, rules=[Rule(tool_name="ha_x")]).model_dump(mode="json")
    assert c.put("/api/policy/config", json=body).status_code == 200
    assert c.get("/api/policy/config").json()["enabled"] is True


def test_put_config_validation_error_returns_400(tmp_path):
    c = make_app(tmp_path, ApprovalQueue())
    r = c.put("/api/policy/config", json={"enabled": "not-a-bool"})
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
