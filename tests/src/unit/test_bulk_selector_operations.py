"""The operation rows a resolved bulk selector hands to dispatch."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import BulkSelectorResolution

from ._bulk_selector_core import add_entity, resolve
from ._bulk_selector_core import salon as salon


async def _two_lights_on(
    hass: HomeAssistant, client: HomeAssistantClient, area_id: str, **kwargs: Any
) -> BulkSelectorResolution:
    add_entity(hass, "light.a", area_id=area_id)
    add_entity(hass, "light.b", area_id=area_id)
    return await resolve(
        client, {"domain": "light", "area_ids": [area_id]}, action="on", **kwargs
    )


async def test_one_rows_parameters_change_reaches_no_other_row(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A dispatcher changing one row's parameters in place, even a nested
    value, changes neither another row nor a later read of the rows."""
    result = await _two_lights_on(
        hass, core_client, salon, parameters={"rgb_color": [255, 0, 0]}
    )

    rows = result.operations
    rows[0]["parameters"]["rgb_color"][0] = -1

    assert rows[1]["parameters"]["rgb_color"] == [255, 0, 0]
    for row in result.operations:
        assert row["parameters"]["rgb_color"] == [255, 0, 0]


async def test_callers_parameters_change_after_resolving_reaches_no_row(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """The caller still holding the dict it passed and changing it, even a
    nested value, does not rewrite what the preview showed will dispatch."""
    caller_parameters = {"rgb_color": [255, 0, 0]}
    result = await _two_lights_on(
        hass, core_client, salon, parameters=caller_parameters
    )

    caller_parameters["rgb_color"][0] = 111

    for row in result.operations:
        assert row["parameters"]["rgb_color"] == [255, 0, 0]


async def test_resolution_equality_is_action_sensitive(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Opposite actions over the same entities are different resolutions."""
    add_entity(hass, "light.one", area_id=salon)
    selector = {"domain": "light", "area_ids": [salon]}

    on_result = await resolve(core_client, selector, action="on")
    off_result = await resolve(core_client, selector, action="off")

    assert on_result.resolved_entity_ids == off_result.resolved_entity_ids
    assert on_result != off_result


async def test_resolution_repr_never_contains_a_lock_or_alarm_code(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A code passed in ``parameters`` never reaches a log line or traceback
    that prints the resolution."""
    add_entity(hass, "lock.one", area_id=salon)

    result = await resolve(
        core_client,
        {"domain": "lock", "area_ids": [salon]},
        action="open",
        parameters={"code": "SENTINEL-SECRET-1234"},
    )

    assert result.operations[0]["parameters"] == {"code": "SENTINEL-SECRET-1234"}
    assert "SENTINEL-SECRET-1234" not in repr(result)


async def test_action_is_normalized_case_insensitively(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Action normalization accepts surrounding whitespace and mixed case."""
    add_entity(hass, "light.one", area_id=salon)

    result = await resolve(
        core_client, {"domain": "light", "area_ids": [salon]}, action=" OFF "
    )

    assert result.operations[0]["action"] == "off"
