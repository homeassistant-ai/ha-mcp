from __future__ import annotations

from types import SimpleNamespace

import pytest

from ha_mcp.tools import tools_service
from ha_mcp.tools.tools_service import (
    _QUICK_ACTION_CACHE,
    _normalize_quick_action_confidence,
    _normalize_quick_action_terms,
    register_service_tools,
)


pytestmark = pytest.mark.unit


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, func):
        self.tools[func.__name__] = func
        return func


class DummyClient:
    def __init__(self, states: list[dict[str, object]], entity_states: dict[str, dict[str, object]] | None = None) -> None:
        self._states = states
        self._entity_states = entity_states or {}
        self.call_service_calls: list[tuple[str, str, dict[str, object]]] = []

    async def get_states(self) -> list[dict[str, object]]:
        return self._states

    async def get_entity_state(self, entity_id: str) -> dict[str, object]:
        if entity_id not in self._entity_states:
            raise RuntimeError(f"Entity {entity_id} not found")
        return self._entity_states[entity_id]

    async def call_service(self, domain: str, service: str, data: dict[str, object]) -> list[dict[str, object]]:
        self.call_service_calls.append((domain, service, data.copy()))
        return [{"domain": domain, "service": service, "entity_id": data.get("entity_id")}]  # type: ignore[arg-type]


class DummySearcher:
    def __init__(self, scores: dict[tuple[str, str], float] | None = None, suggestions: list[str] | None = None) -> None:
        self.scores = scores or {}
        self.suggestions = suggestions or []

    def _calculate_entity_score(self, entity_id: str, friendly_name: str, domain: str, query: str) -> float:  # noqa: ARG002
        return self.scores.get((entity_id, query), 0.0)

    def _get_match_type(self, entity_id: str, friendly_name: str, domain: str, query: str) -> str:  # noqa: ARG002
        return f"match::{entity_id}"

    def get_smart_suggestions(self, entities: list[dict[str, object]], query: str) -> list[str]:  # noqa: ARG002
        return list(self.suggestions)


class DummyDeviceTools:
    async def get_device_operation_status(self, operation_id: str, timeout_seconds: int = 10):  # noqa: ARG002
        return {"operation_id": operation_id, "status": "completed"}

    async def bulk_device_control(self, operations, parallel: bool = True):  # noqa: ANN001, ARG002
        return {"operations": operations, "parallel": parallel}

    async def get_bulk_operation_status(self, operation_ids):  # noqa: ANN001
        return {"operation_ids": operation_ids}


@pytest.fixture(autouse=True)
def clear_quick_action_cache():
    _QUICK_ACTION_CACHE.clear()
    yield
    _QUICK_ACTION_CACHE.clear()


def _setup_tool(monkeypatch: pytest.MonkeyPatch, client: DummyClient, searcher: DummySearcher):
    dummy_settings = SimpleNamespace(fuzzy_threshold=45)
    monkeypatch.setattr(tools_service, "get_global_settings", lambda: dummy_settings)
    monkeypatch.setattr(tools_service, "create_fuzzy_searcher", lambda threshold: searcher)

    mcp = DummyMCP()
    device_tools = DummyDeviceTools()
    register_service_tools(mcp, client, device_tools)
    return mcp.tools["ha_quick_service_action"]


def test_normalize_quick_action_terms_weighted_inputs():
    normalized = _normalize_quick_action_terms([
        {"value": "Kitchen", "weight": 2},
        {"value": "Light", "weight": 1},
    ])

    assert len(normalized) == 2
    assert normalized[0]["value"] == "Kitchen"
    assert pytest.approx(sum(item["weight"] for item in normalized), abs=1e-6) == 1.0
    assert normalized[0]["original_weight"] == pytest.approx(2.0)
    assert normalized[0]["weight"] == pytest.approx(2 / 3)

    with pytest.raises(ValueError):
        _normalize_quick_action_terms("")


def test_normalize_quick_action_confidence_accepts_ratio_and_percent():
    ratio, percent = _normalize_quick_action_confidence(0.6)

    assert ratio == pytest.approx(0.6)
    assert percent == pytest.approx(60.0)

    ratio, percent = _normalize_quick_action_confidence(60)

    assert ratio == pytest.approx(0.6)
    assert percent == pytest.approx(60.0)

    with pytest.raises(ValueError):
        _normalize_quick_action_confidence(150)


@pytest.mark.asyncio
async def test_quick_service_action_auto_executes_high_confidence(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling Light"},
        },
        {
            "entity_id": "light.hallway",
            "state": "off",
            "attributes": {"friendly_name": "Hallway Lamp"},
        },
    ]

    searcher = DummySearcher(
        scores={
            ("light.kitchen", "kitchen lights"): 96,
            ("light.hallway", "kitchen lights"): 10,
        }
    )
    client = DummyClient(states)
    tool = _setup_tool(monkeypatch, client, searcher)

    result = await tool(
        domain="light",
        service="turn_off",
        search_terms="Kitchen Lights",
        min_confidence=80,
        entity_domain=None,
        cache_key=None,
    )

    assert result["success"] is True
    assert result["entity_id"] == "light.kitchen"
    assert result["search_context"]["source"] == "auto"
    assert result["search_context"]["matches_considered"]
    assert client.call_service_calls == [
        ("light", "turn_off", {"entity_id": "light.kitchen"})
    ]


@pytest.mark.asyncio
async def test_quick_service_action_accepts_ratio_min_confidence(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling Light"},
        }
    ]

    searcher = DummySearcher(scores={("light.kitchen", "kitchen lights"): 72})
    client = DummyClient(states)
    tool = _setup_tool(monkeypatch, client, searcher)

    result = await tool(
        domain="light",
        service="turn_off",
        search_terms="Kitchen Lights",
        min_confidence=0.6,
        entity_domain=None,
        cache_key=None,
    )

    assert result["success"] is True
    assert result["confidence_threshold_percent"] == pytest.approx(60.0)
    assert result["confidence_threshold_ratio"] == pytest.approx(0.6, abs=1e-4)
    assert client.call_service_calls == [
        ("light", "turn_off", {"entity_id": "light.kitchen"})
    ]


@pytest.mark.asyncio
async def test_quick_service_action_requests_elicitation_when_no_matches(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling Light"},
        }
    ]

    searcher = DummySearcher(scores={}, suggestions=["Try different keywords"])
    client = DummyClient(states)
    tool = _setup_tool(monkeypatch, client, searcher)

    result = await tool(
        domain="light",
        service="turn_off",
        search_terms="Bedroom Lamp",
        min_confidence=50,
        entity_domain=None,
        cache_key=None,
    )

    assert result["success"] is False
    assert result.get("needs_elicitation") is True
    assert result.get("reason") == "no_matches"
    assert result["elicitation"]["next_call"]["parameters"]["retry_count"] == 1
    assert client.call_service_calls == []


@pytest.mark.asyncio
async def test_quick_service_action_respects_user_decline(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling Light"},
        }
    ]

    searcher = DummySearcher(scores={("light.kitchen", "kitchen"): 80})
    client = DummyClient(states, entity_states={"light.kitchen": states[0]})
    tool = _setup_tool(monkeypatch, client, searcher)

    result = await tool(
        domain="light",
        service="turn_on",
        search_terms="Kitchen",
        selected_entity_id="light.kitchen",
        confirm=False,
        entity_domain=None,
        cache_key=None,
    )

    assert result["success"] is False
    assert result.get("cancelled") is True
    assert "Selection cancelled" in result["message"]
    assert client.call_service_calls == []


@pytest.mark.asyncio
async def test_quick_service_action_rejects_invalid_min_confidence(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen Ceiling Light"},
        }
    ]

    searcher = DummySearcher(scores={("light.kitchen", "kitchen"): 90})
    client = DummyClient(states)
    tool = _setup_tool(monkeypatch, client, searcher)

    result = await tool(
        domain="light",
        service="turn_off",
        search_terms="Kitchen",
        min_confidence=150,
        entity_domain=None,
        cache_key=None,
    )

    assert result["success"] is False
    assert "min_confidence must be integer" in result["error"]
    assert result["min_confidence"] == 150
    assert client.call_service_calls == []
