"""``ha_config_set_helper`` rules that are Core's to decide, on a real ``hass``.

A new tag's id, what an ``input_datetime`` needs, and which names collide with
an existing helper are Core's own answers here, reached through the server's
client and the component's commands.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._core_client import core_backed_client


@pytest.fixture
async def tools(
    hass: HomeAssistant, hass_ws_client: Any, hass_client: Any
) -> AsyncIterator[HelperConfigTools]:
    async with core_backed_client(
        hass,
        hass_ws_client,
        hass_client,
        domains=("input_boolean", "input_datetime", "tag"),
        component=True,
    ) as client:
        yield HelperConfigTools(client)
        await hass.async_block_till_done()


async def test_a_tag_created_without_an_id_gets_the_id_core_generates(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """The tool sends no id of its own making: Core's is a UUID in its
    canonical form, as a tag made in the HA UI gets."""
    await tools.ha_config_set_helper(helper_type="tag", name="Front Door")

    (tag,) = [
        entry for entry in er.async_get(hass).entities.values() if entry.domain == "tag"
    ]
    assert str(uuid.UUID(tag.unique_id)) == tag.unique_id


async def test_an_input_datetime_without_date_or_time_is_cores_to_refuse(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """Core requires one of the two and says so; the tool does not pick."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(helper_type="input_datetime", name="Wake")

    assert "at least a date or a time" in str(exc_info.value)
    assert hass.states.async_entity_ids("input_datetime") == []

    await tools.ha_config_set_helper(
        helper_type="input_datetime", name="Wake", config={"has_time": True}
    )

    state = hass.states.get("input_datetime.wake")
    assert state is not None
    assert state.attributes["has_time"] is True
    assert state.attributes["has_date"] is False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            {"helper_type": "input_boolean", "name": "Night Mode"},
            {"helper_type": "input_boolean", "name": "night-mode!"},
        ),
        (
            {
                "helper_type": "template",
                "menu_choice": "sensor",
                "config": {"name": "Flow Probe", "state": "{{ 1 }}"},
            },
            {
                "helper_type": "template",
                "menu_choice": "sensor",
                "config": {"name": "flow_probe", "state": "{{ 2 }}"},
            },
        ),
    ],
    ids=["storage", "flow"],
)
async def test_a_name_core_would_number_after_an_existing_helper_is_refused(
    hass: HomeAssistant,
    tools: HelperConfigTools,
    first: dict[str, Any],
    second: dict[str, Any],
) -> None:
    """A name that differs only in case or punctuation gets the same id from
    Core, which would create ``..._2`` beside the helper the caller meant."""
    await tools.ha_config_set_helper(**first)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(**second)

    error = json.loads(str(exc_info.value))
    assert "already exists" in error["error"]["message"]
    domain = "sensor" if first["helper_type"] == "template" else "input_boolean"
    assert len(hass.states.async_entity_ids(domain)) == 1
