"""Compact ``ha_call_service`` results (issue #1446).

The OP reported that ha_call_service returns a state record for every entity
affected by a service call — for nested HA-native groups with a WLED member,
that's 16 state objects with a ~250-entry effect_list emitted four times. By
default the tool trims this to the targeted entity's record with heavy
attributes stripped; ``verbose=True`` and
``result_fields``/``result_attribute_keys`` give explicit control.

The tool calls a service on a real ``hass`` through the server's own client.
The service sets the states the way a group's propagation does, in the call's
context, so the changed-state records come from Core's own service-call reply.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools

DOMAIN = "propagation"
SERVICE = "fire"
ATTRIBUTES = {"brightness": 51, "color_mode": "hs", "rgb_color": [255, 121, 0]}
EFFECT_LIST = [f"Effect {index}" for index in range(250)]


def wled_attributes() -> dict[str, Any]:
    return {**ATTRIBUTES, "effect_list": EFFECT_LIST}


def register_propagation(
    hass: HomeAssistant, changes: list[tuple[str, dict[str, Any]]]
) -> None:
    """A service that changes ``changes`` (entity_id, attributes) in its context."""

    async def handler(call: ServiceCall) -> None:
        for entity_id, attributes in changes:
            hass.states.async_set(entity_id, "on", attributes, context=call.context)

    hass.services.async_register(DOMAIN, SERVICE, handler)


@pytest.fixture
def call_service(
    core_client: HomeAssistantClient,
) -> Any:
    tools = ServiceTools(core_client, DeviceControlTools(client=core_client))

    async def call(**arguments: Any) -> dict[str, Any]:
        result: dict[str, Any] = await tools.ha_call_service(
            domain=DOMAIN, service=SERVICE, wait=False, **arguments
        )
        return result

    return call


def as_reported(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    assert state is not None
    record: dict[str, Any] = json.loads(state.as_dict_json)
    return record


async def test_default_keeps_only_the_targeted_entity_of_a_propagation_chain(
    hass: HomeAssistant, call_service: Any
) -> None:
    """OP case: leaf entity targeted, parent groups propagated — keep only target."""
    register_propagation(
        hass,
        [
            ("light.bedroom_desk_inner_left", ATTRIBUTES),
            ("light.bedroom_desk", ATTRIBUTES),
            ("light.bedroom_desk_lights", ATTRIBUTES),
            ("light.lights_in_bedroom", wled_attributes()),
        ],
    )

    response = await call_service(entity_id="light.bedroom_desk_inner_left")

    assert [record["entity_id"] for record in response["result"]] == [
        "light.bedroom_desk_inner_left"
    ]
    assert "warnings" not in response


async def test_default_strips_context_and_timestamps(
    hass: HomeAssistant, call_service: Any
) -> None:
    register_propagation(hass, [("light.kitchen", ATTRIBUTES)])

    response = await call_service(entity_id="light.kitchen")

    assert response["result"] == [
        {"entity_id": "light.kitchen", "state": "on", "attributes": ATTRIBUTES}
    ]


async def test_default_strips_heavy_attribute_lists(
    hass: HomeAssistant, call_service: Any
) -> None:
    """WLED's ~250-entry effect_list and a Hue room's hue_scenes are dropped."""
    register_propagation(
        hass,
        [
            (
                "light.living_room",
                {**wled_attributes(), "hue_scenes": ["Energize", "Relax"]},
            )
        ],
    )

    response = await call_service(entity_id="light.living_room")

    assert response["result"][0]["attributes"] == ATTRIBUTES


@pytest.mark.parametrize(
    "entity_id",
    [
        pytest.param("light.a,light.b", id="comma-separated"),
        pytest.param("light.a, light.b ,,", id="whitespace-and-empties"),
    ],
)
async def test_default_keeps_every_entity_of_a_comma_separated_target(
    hass: HomeAssistant, call_service: Any, entity_id: str
) -> None:
    register_propagation(
        hass,
        [
            ("light.a", ATTRIBUTES),
            ("light.b", ATTRIBUTES),
            ("light.parent_group", ATTRIBUTES),
            ("light.c", ATTRIBUTES),
        ],
    )

    response = await call_service(entity_id=entity_id)

    assert {record["entity_id"] for record in response["result"]} == {
        "light.a",
        "light.b",
    }


async def test_default_keeps_every_record_when_the_target_did_not_change(
    hass: HomeAssistant, call_service: Any
) -> None:
    """If Core only reports propagated parents, they all stay (never [])."""
    register_propagation(
        hass,
        [("light.parent_group", ATTRIBUTES), ("light.grandparent_group", ATTRIBUTES)],
    )

    response = await call_service(entity_id="light.nonexistent_target")

    assert {record["entity_id"] for record in response["result"]} == {
        "light.parent_group",
        "light.grandparent_group",
    }


async def test_default_without_a_target_keeps_every_record_compacted(
    hass: HomeAssistant, call_service: Any
) -> None:
    """Domain-wide calls — the agent expects every affected entity."""
    register_propagation(hass, [("light.a", ATTRIBUTES), ("light.b", ATTRIBUTES)])

    response = await call_service()

    assert response["result"] == [
        {"entity_id": "light.a", "state": "on", "attributes": ATTRIBUTES},
        {"entity_id": "light.b", "state": "on", "attributes": ATTRIBUTES},
    ]


async def test_a_call_that_changes_nothing_returns_an_empty_result(
    hass: HomeAssistant, call_service: Any
) -> None:
    register_propagation(hass, [])

    response = await call_service(entity_id="light.kitchen")

    assert response["result"] == []


@pytest.mark.parametrize(
    "projection",
    [
        pytest.param({}, id="verbose"),
        pytest.param(
            {"result_fields": ["entity_id"], "result_attribute_keys": ["brightness"]},
            id="verbose-overrides-projection",
        ),
    ],
)
async def test_verbose_returns_cores_records_unchanged(
    hass: HomeAssistant, call_service: Any, projection: dict[str, Any]
) -> None:
    register_propagation(
        hass,
        [("light.target", wled_attributes()), ("light.parent_group", ATTRIBUTES)],
    )

    response = await call_service(entity_id="light.target", verbose=True, **projection)

    assert response["result"] == [
        as_reported(hass, "light.target"),
        as_reported(hass, "light.parent_group"),
    ]
    assert "warnings" not in response


async def test_result_fields_project_every_record_without_compaction(
    hass: HomeAssistant, call_service: Any
) -> None:
    register_propagation(
        hass, [("light.target", ATTRIBUTES), ("light.parent_group", ATTRIBUTES)]
    )

    response = await call_service(
        entity_id="light.target", result_fields=["entity_id", "state"]
    )

    assert response["result"] == [
        {"entity_id": "light.target", "state": "on"},
        {"entity_id": "light.parent_group", "state": "on"},
    ]
    assert "warnings" not in response


async def test_result_attribute_keys_narrow_the_attributes(
    hass: HomeAssistant, call_service: Any
) -> None:
    register_propagation(hass, [("light.target", wled_attributes())])

    response = await call_service(
        entity_id="light.target", result_attribute_keys=["brightness", "rgb_color"]
    )

    assert len(response["result"]) == 1
    assert response["result"][0]["attributes"] == {
        "brightness": 51,
        "rgb_color": [255, 121, 0],
    }
    assert "warnings" not in response


async def test_result_fields_and_attribute_keys_apply_together(
    hass: HomeAssistant, call_service: Any
) -> None:
    register_propagation(hass, [("light.target", ATTRIBUTES)])

    response = await call_service(
        entity_id="light.target",
        result_fields=["entity_id", "attributes"],
        result_attribute_keys=["brightness"],
    )

    assert response["result"] == [
        {"entity_id": "light.target", "attributes": {"brightness": 51}}
    ]
    assert "warnings" not in response


async def test_unknown_attribute_key_warns_once_for_every_record(
    hass: HomeAssistant, call_service: Any
) -> None:
    """The same typo across N records emits one warning, not N."""
    register_propagation(
        hass,
        [("light.a", ATTRIBUTES), ("light.b", ATTRIBUTES), ("light.c", ATTRIBUTES)],
    )

    response = await call_service(result_attribute_keys=["nonexistent_attr"])

    assert [record["attributes"] for record in response["result"]] == [{}, {}, {}]
    assert len(response["warnings"]) == 1
    assert "nonexistent_attr" in response["warnings"][0]


async def test_attribute_keys_without_attributes_in_fields_warn_they_were_ignored(
    hass: HomeAssistant, call_service: Any
) -> None:
    """Mirrors ``ha_get_state``'s ``attribute_keys_no_effect`` warning."""
    register_propagation(hass, [("light.target", ATTRIBUTES)])

    response = await call_service(
        entity_id="light.target",
        result_fields=["entity_id", "state"],
        result_attribute_keys=["brightness"],
    )

    assert response["result"] == [{"entity_id": "light.target", "state": "on"}]
    assert len(response["warnings"]) == 1
    assert "result_attribute_keys was ignored" in response["warnings"][0]
