from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.tools import tools_script_runner
from ha_mcp.tools.tools_script_runner import register_script_runner_tools


pytestmark = pytest.mark.unit


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, func):  # noqa: ANN001
        self.tools[func.__name__] = func
        return func


class DummyClient:
    def __init__(self, states: list[dict[str, Any]]) -> None:
        self._states = states
        self.call_service_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get_states(self) -> list[dict[str, Any]]:
        return self._states

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.call_service_calls.append((domain, service, data.copy()))
        return [{"domain": domain, "service": service, "data": data.copy()}]


class DummySearcher:
    def __init__(
        self, scores: dict[tuple[str, str], float] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        self._scores = scores or {}
        self._suggestions = suggestions or []

    def _calculate_entity_score(  # noqa: ANN001
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> float:
        return self._scores.get((entity_id, query), 0.0)

    def _get_match_type(  # noqa: ANN001
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> str:
        return f"match::{entity_id}::{query}"

    def get_smart_suggestions(  # noqa: ANN001
        self, entities: list[dict[str, Any]], query: str
    ) -> list[str]:
        return list(self._suggestions)


@pytest.fixture(autouse=True)
def clear_placeholder_cache():
    tools_script_runner._PLACEHOLDER_SELECTION_CACHE.clear()  # noqa: SLF001
    yield
    tools_script_runner._PLACEHOLDER_SELECTION_CACHE.clear()  # noqa: SLF001


def _setup_tools(
    monkeypatch: pytest.MonkeyPatch,
    client: DummyClient,
    searcher: DummySearcher | None = None,
):
    dummy_settings = SimpleNamespace(fuzzy_threshold=40)
    monkeypatch.setattr(
        tools_script_runner, "get_global_settings", lambda: dummy_settings
    )
    if searcher is not None:
        monkeypatch.setattr(
            tools_script_runner, "create_fuzzy_searcher", lambda threshold: searcher
        )

    mcp = DummyMCP()
    register_script_runner_tools(mcp, client)
    return mcp.tools


@pytest.mark.asyncio
async def test_generate_placeholder_script_normalizes_weights(
    monkeypatch: pytest.MonkeyPatch,
):
    tools = _setup_tools(monkeypatch, DummyClient([]))
    generator = tools["ha_generate_placeholder_script"]

    result = await generator(
        script_id="dynamic_salon_scene",
        alias="Dynamic Salon Scene",
        placeholders=[
            {
                "id": "P_SALON_ALL",
                "domain": "light",
                "search_terms": [
                    {"value": "Salon group", "weight": 2},
                    "Living room lights",
                ],
                "min_confidence": 60,
            }
        ],
        sequence=[{"service": "light.turn_off", "target": {"entity_id": "{{ P_SALON_ALL }}"}}],
        description="Example script",
        additional_fields={
            "MESSAGE": {
                "name": "Notification",
                "required": False,
            }
        },
    )

    assert result["success"] is True
    manifest = result["manifest"]["placeholders"]
    assert manifest[0]["id"] == "P_SALON_ALL"
    weights = [term["weight"] for term in manifest[0]["search_terms"]]
    assert pytest.approx(sum(weights), abs=1e-6) == 1.0
    assert manifest[0]["confidence_threshold_percent"] == pytest.approx(60.0)
    assert "script:" in result["script_yaml"]


@pytest.mark.asyncio
async def test_run_placeholder_script_auto_resolves(monkeypatch: pytest.MonkeyPatch):
    states = [
        {
            "entity_id": "light.salon_all",
            "attributes": {"friendly_name": "Salon All Lights"},
            "state": "off",
        },
        {
            "entity_id": "light.kitchen",
            "attributes": {"friendly_name": "Kitchen Lights"},
            "state": "on",
        },
    ]

    searcher = DummySearcher(
        scores={
            ("light.salon_all", "salon group"): 95.0,
            ("light.kitchen", "salon group"): 10.0,
        }
    )
    client = DummyClient(states)
    tools = _setup_tools(monkeypatch, client, searcher)
    runner = tools["ha_run_placeholder_script"]

    manifest = {
        "placeholders": [
            {
                "id": "P_SALON_ALL",
                "domain": "light",
                "search_terms": ["Salon group"],
                "min_confidence": 70,
            }
        ]
    }

    result = await runner(
        script_id="dynamic_salon_scene",
        placeholder_manifest=manifest,
        fields={"MESSAGE": "Hello"},
    )

    assert result["success"] is True
    resolved = result["resolved_placeholders"]["P_SALON_ALL"]
    assert resolved["entity_id"] == "light.salon_all"
    assert resolved["source"] == "auto"
    assert client.call_service_calls == [
        (
            "script",
            "turn_on",
            {
                "entity_id": "script.dynamic_salon_scene",
                "variables": {
                    "MESSAGE": "Hello",
                    "P_SALON_ALL": "light.salon_all",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_run_placeholder_script_elicitation_with_additional_terms(
    monkeypatch: pytest.MonkeyPatch,
):
    states = [
        {
            "entity_id": "light.dining",
            "attributes": {"friendly_name": "Dining Area Lights"},
            "state": "off",
        }
    ]

    searcher = DummySearcher(
        scores={
            ("light.dining", "salon"): 40.0,
            ("light.dining", "dining"): 95.0,
        }
    )
    client = DummyClient(states)
    tools = _setup_tools(monkeypatch, client, searcher)
    runner = tools["ha_run_placeholder_script"]

    manifest = {
        "placeholders": [
            {
                "id": "P_SALON_ALL",
                "domain": "light",
                "search_terms": ["Salon"],
                "min_confidence": 80,
            }
        ]
    }

    first = await runner(
        script_id="dynamic_salon_scene",
        placeholder_manifest=manifest,
    )

    assert first["needs_elicitation"] is True
    params = first["next_call"]["parameters"]
    params["placeholder_search_terms"].setdefault("P_SALON_ALL", []).append(
        {"value": "dining", "weight": 3}
    )

    second = await runner(**params)

    assert second["success"] is True
    resolved = second["resolved_placeholders"]["P_SALON_ALL"]
    assert resolved["entity_id"] == "light.dining"
    assert client.call_service_calls[0][2]["variables"]["P_SALON_ALL"] == "light.dining"


@pytest.mark.asyncio
async def test_run_placeholder_script_manual_selection(
    monkeypatch: pytest.MonkeyPatch,
):
    states = [
        {
            "entity_id": "light.manual_choice",
            "attributes": {"friendly_name": "Manual Choice"},
            "state": "off",
        }
    ]

    client = DummyClient(states)
    tools = _setup_tools(monkeypatch, client, DummySearcher())
    runner = tools["ha_run_placeholder_script"]

    manifest = {
        "placeholders": [
            {
                "id": "P_CHOICE",
                "domain": "light",
                "search_terms": ["Unrelated"],
                "min_confidence": 90,
            }
        ]
    }

    result = await runner(
        script_id="manual_script",
        placeholder_manifest=manifest,
        placeholder_selections={"P_CHOICE": "light.manual_choice"},
    )

    assert result["success"] is True
    resolved = result["resolved_placeholders"]["P_CHOICE"]
    assert resolved["source"] == "manual"
    assert resolved["entity_id"] == "light.manual_choice"


@pytest.mark.asyncio
async def test_run_placeholder_script_elicitation_failure_after_two_rounds(
    monkeypatch: pytest.MonkeyPatch,
):
    states = [
        {
            "entity_id": "light.low_score",
            "attributes": {"friendly_name": "Low Score"},
            "state": "off",
        }
    ]

    searcher = DummySearcher(
        scores={("light.low_score", "salon"): 30.0, ("light.low_score", "extra"): 35.0}
    )
    client = DummyClient(states)
    tools = _setup_tools(monkeypatch, client, searcher)
    runner = tools["ha_run_placeholder_script"]

    manifest = {
        "placeholders": [
            {
                "id": "P_FAIL",
                "domain": "light",
                "search_terms": ["Salon"],
                "min_confidence": 70,
            }
        ]
    }

    first = await runner(
        script_id="failure_script",
        placeholder_manifest=manifest,
    )

    assert first["needs_elicitation"] is True
    params = first["next_call"]["parameters"]
    params["placeholder_search_terms"].setdefault("P_FAIL", []).append("extra")

    second = await runner(**params)
    assert second["needs_elicitation"] is True
    third_params = second["next_call"]["parameters"]

    failure = await runner(**third_params)

    assert failure["success"] is False
    assert failure["placeholder_id"] == "P_FAIL"
    assert "Unable to resolve" in failure["error"]
    assert client.call_service_calls == []
