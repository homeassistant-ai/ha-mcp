"""``fields=`` and ``attribute_keys=`` projection in ha_get_state (issue #1199).

The tool reads the entity from a real ``hass`` through the component's
``states`` command, so every record it projects is Core's own state with the
live ``last_reported``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

ENTITY_ID = "light.kitchen"
ATTRIBUTES = {"brightness": 200, "color_temp": 3500, "friendly_name": "Kitchen Light"}


@pytest.fixture
def search_tools(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> SearchTools:
    hass.states.async_set(ENTITY_ID, "on", ATTRIBUTES)
    return SearchTools(
        component_core_client, SmartSearchTools(client=component_core_client)
    )


async def get_state(search_tools: SearchTools, **projection: Any) -> dict[str, Any]:
    result: dict[str, Any] = await search_tools.ha_get_state(
        entity_id=ENTITY_ID, **projection
    )
    return result


async def test_no_projection_returns_cores_whole_record(
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state is not None

    record = json.loads(state.as_dict_json)

    result = await get_state(search_tools)

    # The timestamps come back in the instance's timezone, so only their
    # presence is compared.
    assert set(result["data"]) == set(record)
    for key in ("entity_id", "state", "attributes", "context"):
        assert result["data"][key] == record[key]
    assert "warnings" not in result


async def test_fields_keep_only_the_requested_keys(search_tools: SearchTools) -> None:
    result = await get_state(search_tools, fields=["state", "entity_id"])

    assert result["data"] == {"state": "on", "entity_id": ENTITY_ID}
    assert "warnings" not in result


async def test_unknown_field_is_dropped_silently(search_tools: SearchTools) -> None:
    result = await get_state(search_tools, fields=["nonexistent"])

    assert result["data"] == {}
    assert "warnings" not in result


async def test_attribute_keys_narrow_the_attributes(search_tools: SearchTools) -> None:
    result = await get_state(search_tools, attribute_keys=["brightness"])

    assert result["data"]["attributes"] == {"brightness": 200}
    assert result["data"]["state"] == "on"
    assert "warnings" not in result


async def test_attribute_keys_matching_nothing_warn_with_the_available_keys(
    search_tools: SearchTools,
) -> None:
    result = await get_state(search_tools, attribute_keys=["nonexistent_attr"])

    assert result["data"]["attributes"] == {}
    assert len(result["warnings"]) == 1
    assert "attribute_keys" in result["warnings"][0]
    assert "brightness" in result["warnings"][0]


async def test_empty_attribute_keys_ask_for_no_attributes_without_a_warning(
    search_tools: SearchTools,
) -> None:
    # An explicit empty list is a deliberate "keep nothing", not a typo.
    result = await get_state(search_tools, attribute_keys=[])

    assert result["data"]["attributes"] == {}
    assert "warnings" not in result


async def test_partly_matching_attribute_keys_do_not_warn(
    search_tools: SearchTools,
) -> None:
    result = await get_state(search_tools, attribute_keys=["brightness", "typo_key"])

    assert result["data"]["attributes"] == {"brightness": 200}
    assert "warnings" not in result


async def test_fields_and_attribute_keys_apply_together(
    search_tools: SearchTools,
) -> None:
    result = await get_state(
        search_tools, fields=["state", "attributes"], attribute_keys=["brightness"]
    )

    assert result["data"] == {"state": "on", "attributes": {"brightness": 200}}
    assert "warnings" not in result


async def test_attribute_keys_outside_fields_only_warn_that_they_were_ignored(
    search_tools: SearchTools,
) -> None:
    # The record has no attributes to filter, so no typo-guard warning joins
    # the one saying attribute_keys had no effect.
    result = await get_state(search_tools, fields=["state"], attribute_keys=["nope"])

    assert result["data"] == {"state": "on"}
    assert len(result["warnings"]) == 1
    assert "attribute_keys was ignored" in result["warnings"][0]
