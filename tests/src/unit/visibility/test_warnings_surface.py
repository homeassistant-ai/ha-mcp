"""The visibility "warnings last mile".

Covers ``merge_visibility_warnings`` directly and asserts a degradation warning
actually reaches the tool *response* (not just the resolver return) on two of the
collection-read seams — exact-match search and system overview. The area path's
forwarding is covered separately by
``test_search_filter.py::test_area_search_forwards_visibility_warnings_to_response``
(the seam whose silent drop first surfaced the gap). A corrupt config is the
cheapest trigger: ``load_hidden_set`` fails open with a warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.tools.util_helpers import merge_visibility_warnings
from ha_mcp.visibility import resolver

_CORRUPT = "{ corrupt"
_LOAD_FAIL = "could not be loaded"


# --- merge_visibility_warnings (direct) -------------------------------------


def test_merge_visibility_warnings_creates_list_and_returns_same_response():
    resp = {"success": True}
    out = merge_visibility_warnings(resp, ["w1"])
    assert out is resp  # returned for return-composition
    assert out["warnings"] == ["w1"]


def test_merge_visibility_warnings_extends_existing_list():
    resp = {"success": True, "warnings": ["pre"]}
    merge_visibility_warnings(resp, ["w1", "w2"])
    assert resp["warnings"] == ["pre", "w1", "w2"]


def test_merge_visibility_warnings_empty_is_noop():
    resp = {"success": True}
    merge_visibility_warnings(resp, [])
    assert "warnings" not in resp


# --- response-level: a corrupt config surfaces a warning at each seam --------


@pytest.fixture
async def light_a(hass: HomeAssistant) -> None:
    """One registered light with a state, and the registry commands served."""
    assert await async_setup_component(hass, "config", {})
    er.async_get(hass).async_get_or_create(
        "light", "test", "a", suggested_object_id="a"
    )
    hass.states.async_set("light.a", "on")


async def test_exact_match_search_surfaces_visibility_load_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    light_a: None,
) -> None:
    (tmp_path / "entity_visibility.json").write_text(_CORRUPT)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    tools = SearchTools(core_client, SmartSearchTools(client=core_client))
    res = await tools.ha_search(query="a", exact_match=True)
    assert any(_LOAD_FAIL in w for w in res.get("warnings", []))


async def test_system_overview_surfaces_visibility_load_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    light_a: None,
) -> None:
    (tmp_path / "entity_visibility.json").write_text(_CORRUPT)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    res = await SmartSearchTools(client=core_client).get_system_overview(
        detail_level="full"
    )
    assert any(_LOAD_FAIL in w for w in res.get("warnings", []))
