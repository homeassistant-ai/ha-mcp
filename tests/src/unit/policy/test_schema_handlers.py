"""Schema + value-source handlers for the predicate-builder UI (#966)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.policy.value_sources import _cache


@pytest.fixture(autouse=True)
def _clear_value_cache():
    """Value-source fetchers cache responses for 30s; tests want isolation."""
    _cache.clear()
    yield
    _cache.clear()


def _make_fake_tool(
    name: str,
    *,
    parameters: dict,
    read_only: bool = False,
    destructive: bool = False,
) -> SimpleNamespace:
    """Stand-in for a FastMCP Tool object — only the attrs the handler reads."""
    annotations = SimpleNamespace(
        readOnlyHint=read_only or None,
        destructiveHint=destructive or None,
    )
    return SimpleNamespace(
        name=name,
        parameters=parameters,
        annotations=annotations,
    )


def _make_server(*, tools: list, client: MagicMock | None = None) -> MagicMock:
    server = MagicMock()
    server.mcp.local_provider._list_tools = AsyncMock(return_value=tools)
    server.client = client or MagicMock()
    return server


def _make_app(tmp_path: Path, server) -> TestClient:
    h = build_policy_handlers(data_dir=tmp_path, queue=ApprovalQueue(), server=server)
    return TestClient(
        Starlette(
            routes=[
                Route(
                    "/api/policy/tool-schema",
                    h["policy_get_tool_schema"],
                    methods=["GET"],
                ),
                Route(
                    "/api/policy/value-source",
                    h["policy_get_value_source"],
                    methods=["GET"],
                ),
            ]
        )
    )


# --- tool-schema ---


def test_tool_schema_missing_name_returns_400(tmp_path):
    c = _make_app(tmp_path, _make_server(tools=[]))
    r = c.get("/api/policy/tool-schema")
    assert r.status_code == 400


def test_tool_schema_no_server_returns_503(tmp_path):
    # Sidecar mode: build_policy_handlers called with server=None
    h = build_policy_handlers(data_dir=tmp_path, queue=ApprovalQueue(), server=None)
    app = TestClient(
        Starlette(
            routes=[
                Route(
                    "/api/policy/tool-schema",
                    h["policy_get_tool_schema"],
                    methods=["GET"],
                )
            ]
        )
    )
    r = app.get("/api/policy/tool-schema?name=ha_call_service")
    assert r.status_code == 503


def test_tool_schema_unknown_tool_returns_404(tmp_path):
    c = _make_app(tmp_path, _make_server(tools=[]))
    r = c.get("/api/policy/tool-schema?name=ha_nonexistent")
    assert r.status_code == 404


def test_tool_schema_read_only_returns_empty_paths(tmp_path):
    tool = _make_fake_tool(
        "ha_get_state",
        parameters={"properties": {"entity_id": {"type": "string"}}},
        read_only=True,
    )
    c = _make_app(tmp_path, _make_server(tools=[tool]))
    r = c.get("/api/policy/tool-schema?name=ha_get_state")
    assert r.status_code == 200
    data = r.json()
    assert data["is_write_or_destructive"] is False
    assert data["paths"] == []
    assert data["value_sources"] == {}


def test_tool_schema_write_tool_returns_paths_and_value_sources(tmp_path):
    tool = _make_fake_tool(
        "ha_call_service",
        parameters={
            "properties": {
                "domain": {"type": "string", "description": "HA domain"},
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["domain", "service"],
        },
        destructive=True,
    )
    c = _make_app(tmp_path, _make_server(tools=[tool]))
    r = c.get("/api/policy/tool-schema?name=ha_call_service")
    assert r.status_code == 200
    data = r.json()
    assert data["is_write_or_destructive"] is True
    paths_by_path = {p["path"]: p for p in data["paths"]}
    assert "args.domain" in paths_by_path
    assert paths_by_path["args.domain"]["required"] is True
    assert paths_by_path["args.data"]["required"] is False
    # Value-source registry maps the well-known paths
    vs = data["value_sources"]
    assert vs["args.domain"] == "ha_domains"
    assert vs["args.service"] == "ha_services"
    assert vs["args.entity_id"] == "ha_entities"
    # args.data has no registry entry → not in value_sources
    assert "args.data" not in vs


def test_tool_schema_propagates_enum_from_jsonschema(tmp_path):
    tool = _make_fake_tool(
        "ha_made_up",
        parameters={
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
        },
        destructive=True,
    )
    c = _make_app(tmp_path, _make_server(tools=[tool]))
    r = c.get("/api/policy/tool-schema?name=ha_made_up")
    paths = {p["path"]: p for p in r.json()["paths"]}
    assert paths["args.mode"]["enum"] == ["fast", "slow"]


# --- value-source ---


def test_value_source_missing_source_returns_400(tmp_path):
    c = _make_app(tmp_path, _make_server(tools=[]))
    r = c.get("/api/policy/value-source")
    assert r.status_code == 400


def test_value_source_unknown_returns_400(tmp_path):
    c = _make_app(tmp_path, _make_server(tools=[]))
    r = c.get("/api/policy/value-source?source=does_not_exist")
    assert r.status_code == 400


def test_value_source_ha_domains_returns_sorted_list(tmp_path):
    client = MagicMock()
    client.get_services = AsyncMock(
        return_value={"switch": {}, "light": {}, "lock": {}}
    )
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r = c.get("/api/policy/value-source?source=ha_domains")
    assert r.status_code == 200
    assert r.json()["values"] == ["light", "lock", "switch"]


def test_value_source_ha_entities_filterable_by_domain(tmp_path):
    client = MagicMock()
    client.get_states = AsyncMock(
        return_value=[
            {"entity_id": "light.bed"},
            {"entity_id": "switch.fan"},
            {"entity_id": "light.kitchen"},
        ]
    )
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r = c.get("/api/policy/value-source?source=ha_entities&domain=light")
    assert r.json()["values"] == ["light.bed", "light.kitchen"]


def test_value_source_ha_services_filterable_by_domain(tmp_path):
    client = MagicMock()
    client.get_services = AsyncMock(
        return_value={
            "light": {"turn_on": {}, "turn_off": {}},
            "lock": {"lock": {}, "unlock": {}},
        }
    )
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r = c.get("/api/policy/value-source?source=ha_services&domain=lock")
    assert r.json()["values"] == ["lock", "unlock"]


def test_value_source_handles_list_shape_services_payload(tmp_path):
    # Newer HA returns /services as a list of {domain, services} entries.
    client = MagicMock()
    client.get_services = AsyncMock(
        return_value=[
            {"domain": "light", "services": {"turn_on": {}}},
            {"domain": "lock", "services": {"unlock": {}}},
        ]
    )
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r = c.get("/api/policy/value-source?source=ha_domains")
    assert r.json()["values"] == ["light", "lock"]


def test_value_source_fetcher_exception_returns_502(tmp_path):
    client = MagicMock()
    client.get_services = AsyncMock(side_effect=RuntimeError("HA unreachable"))
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r = c.get("/api/policy/value-source?source=ha_domains")
    assert r.status_code == 502
    assert "HA unreachable" in r.json()["error"]


def test_value_source_no_server_returns_503(tmp_path):
    # Sidecar parity with tool-schema: value-source endpoint should
    # 503 when there's no server to introspect HA against.
    h = build_policy_handlers(data_dir=tmp_path, queue=ApprovalQueue(), server=None)
    app = TestClient(
        Starlette(
            routes=[
                Route(
                    "/api/policy/value-source",
                    h["policy_get_value_source"],
                    methods=["GET"],
                )
            ]
        )
    )
    r = app.get("/api/policy/value-source?source=ha_domains")
    assert r.status_code == 503


def test_tool_schema_returns_500_when_list_tools_fails(tmp_path):
    """A FastMCP version bump that breaks `_list_tools()` should surface
    the failure (with logged stack) rather than silently 200 with empty
    paths."""
    server = MagicMock()
    server.mcp.local_provider._list_tools = AsyncMock(
        side_effect=RuntimeError("FastMCP rename broke us")
    )
    c = _make_app(tmp_path, server)
    r = c.get("/api/policy/tool-schema?name=ha_anything")
    assert r.status_code == 500
    assert "FastMCP rename" in r.json()["error"]


def test_value_source_cache_keys_separate_per_params(tmp_path):
    """Cache key includes params: ha_entities?domain=light and
    ha_entities?domain=lock must NOT share a cached result."""
    client = MagicMock()
    # First call returns light entities; second returns nothing on purpose
    # so we'd notice if the second call was served from the first's cache.
    client.get_states = AsyncMock(
        return_value=[
            {"entity_id": "light.bed"},
            {"entity_id": "lock.front"},
        ]
    )
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    r_light = c.get("/api/policy/value-source?source=ha_entities&domain=light")
    r_lock = c.get("/api/policy/value-source?source=ha_entities&domain=lock")
    assert r_light.json()["values"] == ["light.bed"]
    assert r_lock.json()["values"] == ["lock.front"]


def test_value_source_empty_result_not_cached(tmp_path):
    """A transient HA glitch returning [] once must NOT be cached for
    the full TTL window; the next call must re-fetch and reflect real
    state."""
    call_count = {"n": 0}

    async def get_services():
        call_count["n"] += 1
        # First call: empty (glitch); second: real values.
        return {} if call_count["n"] == 1 else {"light": {}, "lock": {}}

    client = MagicMock()
    client.get_services = AsyncMock(side_effect=get_services)
    c = _make_app(tmp_path, _make_server(tools=[], client=client))
    first = c.get("/api/policy/value-source?source=ha_domains").json()
    second = c.get("/api/policy/value-source?source=ha_domains").json()
    assert first["values"] == []
    assert sorted(second["values"]) == ["light", "lock"], (
        "empty result must not be cached; second call should re-fetch real values"
    )
    assert call_count["n"] == 2, "second call must hit the fetcher, not the cache"


def test_tool_schema_handles_malformed_properties(tmp_path):
    """A tool with a non-dict property schema entry (quirky FastMCP
    schema) should be skipped, not crash the endpoint."""
    tool = _make_fake_tool(
        "ha_quirky",
        parameters={
            "properties": {
                "ok": {"type": "string"},
                "broken": "this should be a dict",
            }
        },
        destructive=True,
    )
    c = _make_app(tmp_path, _make_server(tools=[tool]))
    r = c.get("/api/policy/tool-schema?name=ha_quirky")
    assert r.status_code == 200
    paths = {p["path"] for p in r.json()["paths"]}
    assert "args.ok" in paths
    assert "args.broken" not in paths
