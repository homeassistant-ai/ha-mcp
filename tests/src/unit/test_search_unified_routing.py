"""Search modes use the component without exposing legacy-only tuning."""

from typing import Any

import pytest

from ha_mcp.tools import tools_search

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_search_component_routing import (
    _CAPS_SEARCH,
    RoutingClient,
    _build_ha_search,
    _entity_search_result,
    _setup_visibility_disabled,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "mode"),
    [
        ({"domain_filter": "light"}, "domain_listing"),
        ({"state_filter": "on"}, "state_listing"),
        ({"area_filter": "Kitchen"}, "area_only"),
        ({"area_filter": " Kitchen "}, "area_only"),
        ({"query": "kitchen", "area_filter": "Kitchen"}, "area_filtered_query"),
    ],
)
async def test_unified_component_serves_listings_and_locations(
    tmp_path: Any, monkeypatch: Any, arguments: dict[str, Any], mode: str
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    result = {**_entity_search_result(), "area_names": ["Kitchen"]}
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result={
            **_CAPS_SEARCH,
            "capabilities": [*_CAPS_SEARCH["capabilities"], "search_unified"],
        },
        cmd_result=result,
    )
    client = RoutingClient()
    with patch_ws(ws, tools_search):
        response = await _build_ha_search(client)(**arguments)
    assert response["entities"][0]["entity_id"] == "light.kitchen"
    assert response["search_type"] == mode
    assert client.get_states_calls == 0
    assert not client.ws_types
    calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/search"
    ]
    assert len(calls) == 1
    assert calls[0].kwargs["search_types"] == ["entity"]
    if "area_filter" in arguments:
        assert response["area_filter"] == arguments["area_filter"].strip()
        assert response["area_names"] == ["Kitchen"]


@pytest.mark.asyncio
async def test_explicit_config_pin_explains_skipped_entities(
    tmp_path: Any, monkeypatch: Any
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result={"automations": [], "config_total_matches": 0, "partial": False},
    )
    with patch_ws(ws, tools_search):
        response = await _build_ha_search(RoutingClient())(
            query="sensi", domain_filter="switch", search_types=["automation"]
        )
    assert response["entities"] == []
    assert any("entity search skipped" in warning for warning in response["warnings"])


def test_unified_component_large_dashboard_window_is_serviceable() -> None:
    from types import SimpleNamespace

    req = tools_search._ResolvedSearch(
        query="kitchen",
        query_text="kitchen",
        domain_filter=None,
        area_filter=None,
        state_filter=None,
        parsed_search_types=["automation", "dashboard"],
        parsed_fields=None,
        result_fields=None,
        limit=100,
        offset=600,
        exact_match=True,
        include_hidden=True,
        include_config=False,
        group_by_domain=False,
        per_domain_limit=None,
        config_time_budget=None,
        registry_eligible=False,
        body_eligible=True,
        body_skipped_by_intent_gate=False,
    )
    caps = SimpleNamespace(
        capabilities=frozenset({"search_unified"}), limits={"max_results": 500}
    )
    assert tools_search._dashboard_split_serviceable(req, caps)
