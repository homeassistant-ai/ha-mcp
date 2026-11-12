"""The label and category tools on real Core, on an install without the component.

Every row, list and refusal here is Core's: the tools talk to a real ``hass``
through the server's own client, and the tests read the registries back from
Core.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_categories import CategoryTools
from ha_mcp.tools.tools_labels import LabelTools

from ._tool_errors import rejected_call_body


@pytest.fixture
async def labels(hass: HomeAssistant, core_client: HomeAssistantClient) -> LabelTools:
    """The label tools on Core with its ``config`` commands set up."""
    assert await async_setup_component(hass, "config", {})
    return LabelTools(core_client)


@pytest.fixture
async def categories(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> CategoryTools:
    """The category tools on Core with its ``config`` commands set up."""
    assert await async_setup_component(hass, "config", {})
    return CategoryTools(core_client)


async def test_label_update_without_a_name_keeps_the_name(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    """Changing only a label's color does not make the caller restate its name."""
    label = lr.async_get(hass).async_create("Security", color="red")

    result = await labels.ha_config_set_label(label_id=label.label_id, color="blue")

    stored = lr.async_get(hass).async_get_label(label.label_id)
    assert stored is not None
    assert (stored.name, stored.color) == ("Security", "blue")
    assert result["message"] == "Successfully updated label: Security"


async def test_label_update_with_empty_values_clears_them(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    """An empty color, icon or description removes it from the label; before,
    the tool sent the empty value on, which Core refuses for a color or icon."""
    label = lr.async_get(hass).async_create(
        "Security", color="red", icon="mdi:shield", description="Locks and alarms"
    )

    await labels.ha_config_set_label(
        name="Security", label_id=label.label_id, color="", icon="", description=""
    )

    stored = lr.async_get(hass).async_get_label(label.label_id)
    assert stored is not None
    assert (stored.color, stored.icon, stored.description) == (None, None, None)


async def test_label_create_without_a_name_reports_cores_refusal(
    labels: LabelTools,
) -> None:
    """A create without a name is refused with Core's own reason, as an input
    error the caller can fix."""
    error = await rejected_call_body(labels.ha_config_set_label(color="red"))

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "name" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_format"
    assert "ha_config_get_label" in error["error"]["suggestion"]


async def test_label_get_lists_what_carries_and_targets_the_label(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    """Reading one label tells the caller what uses it, so it can see what a
    delete would affect."""
    assert await async_setup_component(hass, "search", {})
    label = lr.async_get(hass).async_create("Security")
    area = ar.async_get(hass).async_create("Hall", labels={label.label_id})
    entity = er.async_get(hass).async_get_or_create("light", "demo", "hall_lamp")
    er.async_get(hass).async_update_entity(entity.entity_id, labels={label.label_id})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "lock_up",
                "alias": "Lock up",
                "triggers": [{"trigger": "event", "event_type": "lock_up"}],
                "actions": [
                    {
                        "action": "light.turn_off",
                        "target": {"label_id": label.label_id},
                    }
                ],
            }
        },
    )

    result = await labels.ha_config_get_label(label_id=label.label_id)

    assert result["related"]["area"] == [area.id]
    assert result["related"]["entity"] == [entity.entity_id]
    assert result["related"]["automation"] == ["automation.lock_up"]
    assert "warnings" not in result


async def test_label_get_without_core_search_says_usage_is_unknown(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    """When Core cannot answer what uses a label, the label is still returned and
    the gap is named instead of reported as unused."""
    label = lr.async_get(hass).async_create("Security")

    result = await labels.ha_config_get_label(label_id=label.label_id)

    assert result["label"]["label_id"] == label.label_id
    assert result["related"] is None
    assert result["warnings"] == ["related unavailable: Unknown command."]


async def test_category_update_without_a_name_keeps_the_name(
    hass: HomeAssistant, categories: CategoryTools
) -> None:
    """Changing only a category's icon does not make the caller restate its name."""
    category = cr.async_get(hass).async_create(scope="automation", name="Lighting")

    result = await categories.ha_config_set_category(
        scope="automation", category_id=category.category_id, icon="mdi:lamp"
    )

    stored = cr.async_get(hass).async_get_category(
        scope="automation", category_id=category.category_id
    )
    assert stored is not None
    assert (stored.name, stored.icon) == ("Lighting", "mdi:lamp")
    assert result["message"] == "Successfully updated category: Lighting"


async def test_category_update_with_an_empty_icon_clears_it(
    hass: HomeAssistant, categories: CategoryTools
) -> None:
    """An empty icon removes it from the category; before, the tool sent the
    empty value on, which Core refuses for an icon."""
    category = cr.async_get(hass).async_create(
        scope="automation", name="Lighting", icon="mdi:lamp"
    )

    await categories.ha_config_set_category(
        name="Lighting", scope="automation", category_id=category.category_id, icon=""
    )

    stored = cr.async_get(hass).async_get_category(
        scope="automation", category_id=category.category_id
    )
    assert stored is not None
    assert stored.icon is None


async def test_category_update_of_an_unknown_id_is_not_found(
    categories: CategoryTools,
) -> None:
    """An update of a category Core does not hold is reported as not found,
    naming the id and scope that were looked up."""
    error = await rejected_call_body(
        categories.ha_config_set_category(
            scope="automation", category_id="missing", icon="mdi:lamp"
        )
    )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "'missing' not found in scope 'automation'" in error["error"]["message"]
    assert any("ha_config_get_category" in s for s in error["error"]["suggestions"])


async def test_category_name_conflict_is_not_reported_as_not_found(
    hass: HomeAssistant, categories: CategoryTools
) -> None:
    """Core quotes a refused name in its message, so a taken name that reads like
    Core's unknown-id text is still reported as the name refusal."""
    registry = cr.async_get(hass)
    registry.async_create(scope="automation", name="Category ID doesn't exist")
    other = registry.async_create(scope="automation", name="Lighting")

    error = await rejected_call_body(
        categories.ha_config_set_category(
            scope="automation",
            category_id=other.category_id,
            name="Category ID doesn't exist",
        )
    )

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "already in use" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_info"
    assert "ha_config_get_category" in error["error"]["suggestion"]


async def test_category_create_without_a_name_reports_cores_refusal(
    categories: CategoryTools,
) -> None:
    """A create without a name is refused with Core's own reason, as an input
    error the caller can fix."""
    error = await rejected_call_body(categories.ha_config_set_category(scope="automation"))

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "name" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_format"
