"""``ha_get_state`` on real Core: single and bulk reads, errors, projections.

States are Core's, set with ``hass.states.async_set``. ``component_core_client``
reads through the component's ``states`` command, ``core_client`` over REST;
which backend serves which call is covered in
``test_ha_get_state_component_routing.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_search import SearchTools

from ._command_faults import received
from ._search_tool import search_tools_on
from ._tool_errors import error_body, error_of

STATES = "ha_mcp_tools/states"
NO_EFFECT = "attribute_keys was ignored"


@pytest.fixture(autouse=True)
def _states(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.kitchen", "on", {"brightness": 255, "color_temp": 3500}
    )
    hass.states.async_set("light.living_room", "off", {})


@pytest.fixture
def tools(
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SearchTools:
    return search_tools_on(component_core_client, tmp_path, monkeypatch)


@pytest.fixture
def rest_tools(
    core_client: HomeAssistantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SearchTools:
    return search_tools_on(core_client, tmp_path, monkeypatch)


def _warnings(resp: dict[str, Any]) -> list[str]:
    assert "warnings" not in resp["data"]
    warnings: list[str] = resp.get("warnings", [])
    return warnings


async def test_a_bulk_read_returns_every_state_with_no_error_keys(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(entity_id=["light.kitchen", "light.living_room"])

    data = resp["data"]
    assert (data["success"], data["count"]) == (True, 2)
    assert data["states"]["light.kitchen"]["state"] == "on"
    assert data["states"]["light.living_room"]["state"] == "off"
    assert not {"errors", "error_count", "partial"} & set(data)


async def test_a_rest_404_in_a_bulk_read_is_a_partial_entity_not_found(
    rest_tools: SearchTools,
) -> None:
    """Core's REST 404 for one id names that id and points at ha_search, while
    the found state is still returned."""
    resp = await rest_tools.ha_get_state(
        entity_id=["light.kitchen", "sensor.nonexistent"]
    )

    data = resp["data"]
    assert (data["success"], data["count"], data["error_count"]) == (True, 1, 1)
    assert data["partial"] is True
    assert data["states"]["light.kitchen"]["state"] == "on"
    (error,) = data["errors"]
    assert error["entity_id"] == "sensor.nonexistent"
    assert error["error"]["code"] == "ENTITY_NOT_FOUND"
    assert "sensor.nonexistent" in error["error"]["message"]
    assert any("ha_search" in s for s in data["suggestions"])


async def test_a_bulk_read_where_every_id_fails_is_unsuccessful_not_partial(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(entity_id=["sensor.bad1", "sensor.bad2"])

    data = resp["data"]
    assert (data["success"], data["count"], data["error_count"]) == (False, 0, 2)
    assert data["states"] == {}
    assert [e["entity_id"] for e in data["errors"]] == ["sensor.bad1", "sensor.bad2"]
    assert "partial" not in data
    assert data["suggestions"]


@pytest.mark.parametrize(
    "entity_id",
    [
        pytest.param([], id="empty"),
        pytest.param(["light.kitchen", 123], id="non-string"),
        pytest.param([f"sensor.s{i}" for i in range(101)], id="over-100"),
    ],
)
async def test_a_malformed_id_list_is_refused_as_entity_id_validation(
    tools: SearchTools, entity_id: list[Any]
) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.ha_get_state(entity_id=entity_id)

    assert error_of(exc)["code"] == "VALIDATION_FAILED"
    assert error_body(exc)["parameter"] == "entity_id"


async def test_a_repeated_id_is_read_once(
    hass: HomeAssistant, tools: SearchTools
) -> None:
    with received(hass, STATES) as seen:
        resp = await tools.ha_get_state(
            entity_id=["light.kitchen", "light.kitchen", "light.kitchen"]
        )

    assert resp["data"]["count"] == 1
    assert [msg["entity_ids"] for msg in seen[STATES]] == [["light.kitchen"]]


async def test_a_bulk_read_core_never_answers_is_a_connection_failure(
    unreachable_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = search_tools_on(unreachable_client, tmp_path, monkeypatch)

    resp = await tools.ha_get_state(entity_id=["light.kitchen"])

    data = resp["data"]
    assert data["success"] is False
    assert data["errors"][0]["error"]["code"] == "CONNECTION_FAILED"


async def test_a_single_rest_read_returns_that_entitys_state(
    core_client: HomeAssistantClient, rest_tools: SearchTools
) -> None:
    with patch.object(
        core_client, "get_entity_state", wraps=core_client.get_entity_state
    ) as rest:
        resp = await rest_tools.ha_get_state(entity_id="light.kitchen")

    assert (resp["data"]["entity_id"], resp["data"]["state"]) == ("light.kitchen", "on")
    rest.assert_awaited_once_with("light.kitchen")


async def test_a_single_rest_404_is_refused_as_entity_not_found(
    rest_tools: SearchTools,
) -> None:
    with pytest.raises(ToolError) as exc:
        await rest_tools.ha_get_state(entity_id="sensor.nonexistent")

    assert error_body(exc)["success"] is False
    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"


async def test_attribute_keys_without_attributes_in_fields_warns_outside_the_record(
    tools: SearchTools,
) -> None:
    """``fields=["state"]`` returns only ``state``; the ignored attribute_keys
    is reported beside the record, not inside it."""
    resp = await tools.ha_get_state(
        entity_id="light.kitchen", fields=["state"], attribute_keys=["brightness"]
    )

    assert resp["data"] == {"state": "on"}
    assert any(NO_EFFECT in w for w in _warnings(resp))


async def test_attribute_keys_with_attributes_in_fields_filters_without_warning(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id="light.kitchen",
        fields=["state", "attributes"],
        attribute_keys=["brightness"],
    )

    assert resp["data"]["attributes"] == {"brightness": 255}
    assert _warnings(resp) == []


async def test_bulk_attribute_keys_without_attributes_in_fields_warns_once(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id=["light.kitchen"], fields=["state"], attribute_keys=["brightness"]
    )

    assert resp["data"]["states"]["light.kitchen"] == {"state": "on"}
    assert len([w for w in _warnings(resp) if NO_EFFECT in w]) == 1


async def test_bulk_attribute_keys_with_attributes_in_fields_filters_without_warning(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id=["light.kitchen"],
        fields=["state", "attributes"],
        attribute_keys=["brightness"],
    )

    assert resp["data"]["states"]["light.kitchen"]["attributes"] == {"brightness": 255}
    assert _warnings(resp) == []


@pytest.mark.parametrize(
    ("kwargs", "parameter"),
    [
        pytest.param({"fields": 123}, "fields", id="fields-not-a-list"),
        pytest.param({"fields": '["'}, "fields", id="fields-bad-json"),
        pytest.param(
            {"attribute_keys": 123}, "attribute_keys", id="attribute-keys-not-a-list"
        ),
    ],
)
async def test_a_malformed_projection_is_refused_naming_its_parameter(
    tools: SearchTools, kwargs: dict[str, Any], parameter: str
) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.ha_get_state(entity_id="light.kitchen", **kwargs)

    assert error_of(exc)["code"] == "VALIDATION_FAILED"
    assert error_body(exc)["parameter"] == parameter


async def test_a_mistyped_attribute_key_warns_with_the_available_keys(
    tools: SearchTools,
) -> None:
    """A typo would otherwise come back as a silent ``attributes: {}``."""
    resp = await tools.ha_get_state(
        entity_id="light.kitchen", attribute_keys=["nonexistent_attr"]
    )

    assert resp["data"]["attributes"] == {}
    (warning,) = _warnings(resp)
    assert "attribute_keys" in warning
    assert "brightness" in warning


async def test_attribute_keys_with_one_match_drop_the_rest_without_warning(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id="light.kitchen", attribute_keys=["brightness", "nonexistent"]
    )

    assert resp["data"]["attributes"] == {"brightness": 255}
    assert _warnings(resp) == []


async def test_a_mistyped_attribute_key_in_a_bulk_read_warns_with_the_available_keys(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id=["light.kitchen"], attribute_keys=["typo_attr"]
    )

    assert resp["data"]["states"]["light.kitchen"]["attributes"] == {}
    (warning,) = _warnings(resp)
    assert "attribute_keys" in warning
    assert "brightness" in warning


async def test_a_matching_attribute_key_in_a_bulk_read_does_not_warn(
    tools: SearchTools,
) -> None:
    resp = await tools.ha_get_state(
        entity_id=["light.kitchen"], attribute_keys=["brightness"]
    )

    assert resp["data"]["states"]["light.kitchen"]["attributes"] == {"brightness": 255}
    assert _warnings(resp) == []
