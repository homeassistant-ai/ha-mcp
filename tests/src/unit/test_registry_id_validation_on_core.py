"""``validate_registry_ids`` on real Core, on an install with the component.

The ids are checked against the rows the component reads from Core's
registries; the tests create those rows in Core.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_registries import WS_REGISTRIES
from ha_mcp.tools.config_helpers.registry import validate_registry_ids

from ._client_spies import sent_commands
from ._routing_core import pooled_socket, sent_types
from ._tool_errors import rejected_call_body


@pytest.fixture(autouse=True)
async def _config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands, which the registries' WebSocket API is part of."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def references(hass: HomeAssistant) -> dict[str, Any]:
    """One row in each registry, and the arguments that name them all."""
    area = ar.async_get(hass).async_create("Hall")
    floor = fr.async_get(hass).async_create("Ground")
    label = lr.async_get(hass).async_create("Guests")
    categories = cr.async_get(hass)
    automation = categories.async_create(scope="automation", name="Night")
    script = categories.async_create(scope="script", name="Morning")
    return {
        "area_id": area.id,
        "labels": [label.label_id],
        "categories": {
            "automation": automation.category_id,
            "script": script.category_id,
        },
        "floor_id": floor.floor_id,
    }


async def _validate(client: HomeAssistantClient, references: dict[str, Any]) -> None:
    await validate_registry_ids(
        client,
        references["area_id"],
        references["labels"],
        references["categories"],
        floor_id=references["floor_id"],
        fail_closed=True,
    )


async def test_the_registries_are_read_in_one_question_to_the_component(
    component_core_client: HomeAssistantClient, references: dict[str, Any]
) -> None:
    """With the component, ids of four registries and two category scopes cost
    one read, and none of Core's list commands is sent."""
    with sent_commands(await pooled_socket(component_core_client)) as sent:
        await _validate(component_core_client, references)

    types = sent_types(sent)
    assert types.count(WS_REGISTRIES) == 1
    assert [name for name in types if name.endswith("_registry/list")] == []


@pytest.mark.parametrize(
    ("unknown", "named"),
    [
        ({"area_id": "missing"}, {"area_id": "missing"}),
        ({"floor_id": "missing"}, {"floor_id": "missing"}),
        ({"labels": ["missing"]}, {"unknown_labels": ["missing"]}),
    ],
)
async def test_an_id_the_component_does_not_list_is_refused(
    component_core_client: HomeAssistantClient,
    references: dict[str, Any],
    unknown: dict[str, Any],
    named: dict[str, Any],
) -> None:
    """A reference to an id the component's rows do not hold is refused, and
    the error names it."""
    error = await rejected_call_body(
        _validate(component_core_client, {**references, **unknown})
    )

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert {key: error[key] for key in named} == named


async def test_a_category_is_looked_up_in_the_rows_of_its_own_scope(
    component_core_client: HomeAssistantClient, references: dict[str, Any]
) -> None:
    """A category id of the automation scope is not one of the script scope,
    though one read returns the rows of both."""
    automation_category = references["categories"]["automation"]

    error = await rejected_call_body(
        _validate(
            component_core_client,
            {
                **references,
                "categories": {
                    "automation": automation_category,
                    "script": automation_category,
                },
            },
        )
    )

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert (error["category"], error["scope"]) == (automation_category, "script")
