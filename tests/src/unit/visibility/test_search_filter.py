"""The visibility filter in ``ha_search``'s entity listings, on real Core.

Each test drives the public ``ha_search`` (or ``ha_get_state``) tool on the
server's client connected to a real ``hass`` without the ha_mcp_tools
component, so the legacy search path reads Core's own states and registry
rows. Count coherence: ``entity_total_matches`` is the filtered total.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig

_DIAGNOSTIC = EntityCategory.DIAGNOSTIC


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> SearchTools:
    """``ha_search`` on a ``hass`` that serves the registry commands."""
    assert await async_setup_component(hass, "config", {})
    return SearchTools(core_client, SmartSearchTools(client=core_client))


@pytest.fixture
def add_entity(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> Callable[..., None]:
    """Register an entity with Core and give it a state."""

    def _add(
        entity_id: str,
        state: str = "on",
        *,
        category: EntityCategory | None = None,
        area_id: str | None = None,
    ) -> None:
        domain, _, object_id = entity_id.partition(".")
        entry = entity_registry.async_get_or_create(
            domain,
            "test",
            object_id,
            suggested_object_id=object_id,
            entity_category=category,
        )
        assert entry.entity_id == entity_id
        if area_id is not None:
            entity_registry.async_update_entity(entity_id, area_id=area_id)
        hass.states.async_set(entity_id, state)

    return _add


@pytest.fixture
def foo_entities(add_entity: Callable[..., None]) -> None:
    add_entity("sensor.foo_batt", "50", category=_DIAGNOSTIC)
    add_entity("light.foo_lamp")


def _ids(res: dict[str, Any]) -> list[str]:
    return sorted(row["entity_id"] for row in res["entities"])


async def test_query_search_hides_category_and_counts_only_visible_matches(
    tools: SearchTools,
    foo_entities: None,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=True, exclude_categories=["diagnostic"])

    res = await tools.ha_search(query="foo", exact_match=True)

    assert _ids(res) == ["light.foo_lamp"]
    assert res["entity_total_matches"] == 1


async def test_query_search_with_filter_disabled_returns_every_match(
    tools: SearchTools,
    foo_entities: None,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=False, exclude_categories=["diagnostic"])

    res = await tools.ha_search(query="foo", exact_match=True)

    assert _ids(res) == ["light.foo_lamp", "sensor.foo_batt"]


async def test_query_search_with_corrupt_config_hides_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: SearchTools,
    foo_entities: None,
) -> None:
    """A config that cannot be read fails open: the search still lists all."""
    (tmp_path / "entity_visibility.json").write_text("{ corrupt")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    res = await tools.ha_search(query="foo", exact_match=True)

    assert _ids(res) == ["light.foo_lamp", "sensor.foo_batt"]


async def test_domain_listing_hides_category_before_counting(
    tools: SearchTools,
    add_entity: Callable[..., None],
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=True, exclude_categories=["diagnostic"])
    add_entity("sensor.keep", "1")
    add_entity("sensor.drop", "2", category=_DIAGNOSTIC)

    res = await tools.ha_search(domain_filter="sensor", include_hidden=True)

    assert _ids(res) == ["sensor.keep"]
    assert res["entity_total_matches"] == 1


async def test_state_listing_hides_category_before_counting(
    tools: SearchTools,
    add_entity: Callable[..., None],
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """The total counts visible entities in the state, not every one in it."""
    set_config(enabled=True, exclude_categories=["diagnostic"])
    add_entity("sensor.on_keep", "on")
    add_entity("sensor.on_keep2", "on")
    add_entity("sensor.on_drop", "on", category=_DIAGNOSTIC)
    add_entity("sensor.off_ignore", "off")

    res = await tools.ha_search(state_filter="on", include_hidden=True)

    assert _ids(res) == ["sensor.on_keep", "sensor.on_keep2"]
    assert res["entity_total_matches"] == 2


async def test_state_listing_matches_mixed_case_state(
    hass: HomeAssistant,
    tools: SearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A state of "Vacation" is found by ``state_filter="vacation"``."""
    set_config(enabled=False)
    hass.states.async_set("input_select.mode", "Vacation")
    hass.states.async_set("input_select.other", "Home")

    res = await tools.ha_search(state_filter="vacation")

    assert _ids(res) == ["input_select.mode"]
    assert res["entity_total_matches"] == 1


async def test_targeted_state_read_returns_entity_the_denylist_hides(
    hass: HomeAssistant,
    tools: SearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """Outside enforce mode a targeted read is not filtered, although the
    same entity is missing from a listing."""
    denied = "input_boolean.denied_probe"
    set_config(enabled=True, deny_entity_ids=[denied])
    hass.states.async_set(denied, "on")

    listing = await tools.ha_search(domain_filter="input_boolean")
    res = await tools.ha_get_state(denied)

    assert _ids(listing) == []
    assert res["data"]["entity_id"] == denied


async def test_area_allowlist_admits_category_the_exclude_would_hide(
    area_registry: ar.AreaRegistry,
    tools: SearchTools,
    add_entity: Callable[..., None],
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An allowed-area match is shown past the broad category exclusion, and
    an entity outside the allowed area stays hidden."""
    kitchen = area_registry.async_create("Kitchen")
    bedroom = area_registry.async_create("Bedroom")
    set_config(
        enabled=True, exclude_categories=["diagnostic"], allow_areas=[kitchen.id]
    )
    add_entity("sensor.foo_diag", "1", category=_DIAGNOSTIC, area_id=kitchen.id)
    add_entity("light.foo_keep", area_id=kitchen.id)
    add_entity("light.foo_other", area_id=bedroom.id)

    res = await tools.ha_search(query="foo", exact_match=True)

    assert _ids(res) == ["light.foo_keep", "sensor.foo_diag"]


async def test_area_search_forwards_visibility_warnings(
    area_registry: ar.AreaRegistry,
    tools: SearchTools,
    add_entity: Callable[..., None],
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A visibility warning raised while listing an area reaches the response."""
    kitchen = area_registry.async_create("Kitchen")
    add_entity("light.kitchen", area_id=kitchen.id)
    set_config(enabled=True, exclude_categories=["typo"])

    res = await tools.ha_search(area_filter="Kitchen")

    assert _ids(res) == ["light.kitchen"]
    assert any("unknown exclude_categories" in w for w in res.get("warnings", []))


async def test_area_search_without_warnings_has_no_warnings_key(
    area_registry: ar.AreaRegistry,
    tools: SearchTools,
    add_entity: Callable[..., None],
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An empty list is omitted, not returned as ``"warnings": []``."""
    kitchen = area_registry.async_create("Kitchen")
    add_entity("light.kitchen", area_id=kitchen.id)
    set_config(enabled=False)

    res = await tools.ha_search(area_filter="Kitchen")

    assert _ids(res) == ["light.kitchen"]
    assert "warnings" not in res
