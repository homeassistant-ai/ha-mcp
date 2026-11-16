"""ha_get_entity's bulk read and unique_id mode, and ha_remove_entity's bulk
removal (issue #1813 phase 0).

The tools run on the server's own client against a real ``hass``: the entity
registry, its commands and every refusal are Core's.
"""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import sent_types
from ._tool_errors import error_of

GET = "config/entity_registry/get"
GET_ENTRIES = "config/entity_registry/get_entries"
REMOVE = "config/entity_registry/remove"
# No dot: Core's schema refuses it as an entity_id.
BAD_ID = "not an id"


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> EntityTools:
    """Entity tools on a ``hass`` with light.a, light.b, sensor.a and sensor.b."""
    assert await async_setup_component(hass, "config", {})
    for domain, object_id in (
        ("light", "a"),
        ("light", "b"),
        ("sensor", "a"),
        ("sensor", "b"),
    ):
        entity_registry.async_get_or_create(
            domain, "demo", f"{domain}_{object_id}", suggested_object_id=object_id
        )
    return EntityTools(core_client)


async def test_a_bulk_read_asks_core_once_for_every_id(
    tools: EntityTools, sent: AsyncMock
) -> None:
    result = await tools.ha_get_entity(entity_id=["light.a", "sensor.b"])

    assert result["success"] is True
    assert result["count"] == 2
    reads = [m.args[0] for m in sent.call_args_list if m.args[0]["type"] == GET_ENTRIES]
    assert [r["entity_ids"] for r in reads] == [["light.a", "sensor.b"]]
    assert GET not in sent_types(sent)


async def test_a_bulk_read_answers_every_id_in_the_order_asked(
    tools: EntityTools,
) -> None:
    """The caller pairs answers with its ids by position: a repeated id gets
    its own answer, though Core's reply maps it once."""
    result = await tools.ha_get_entity(entity_id=["light.b", "light.a", "light.b"])

    assert [e["entity_id"] for e in result["entity_entries"]] == [
        "light.b",
        "light.a",
        "light.b",
    ]


async def test_a_refused_bulk_read_gives_every_id_cores_refusal(
    tools: EntityTools,
) -> None:
    """Core refuses the whole chunk for one bad id: each id carries Core's
    message, not a "not found" that would send the caller to look elsewhere."""
    result = await tools.ha_get_entity(entity_id=["light.a", BAD_ID])

    assert result["count"] == 0
    errors = {e["entity_id"]: e["error"] for e in result["errors"]}
    assert errors.keys() == {"light.a", BAD_ID}
    for message in errors.values():
        assert "entity_ids" in message
        assert "not found" not in message.lower()


async def test_a_single_read_reads_the_one_entry(
    tools: EntityTools, sent: AsyncMock
) -> None:
    result = await tools.ha_get_entity(entity_id="light.a")

    assert result["success"] is True
    assert result["entity_id"] == "light.a"
    assert GET in sent_types(sent)
    assert GET_ENTRIES not in sent_types(sent)


async def test_a_bulk_removal_sorts_each_id_by_cores_answer(
    tools: EntityTools, entity_registry: er.EntityRegistry, sent: AsyncMock
) -> None:
    """Removed, already absent (skipped, so a retry is safe) or refused; one
    removal per id, in the order asked."""
    result = await tools.ha_remove_entity(
        entity_id=["sensor.a", "sensor.missing", BAD_ID]
    )

    assert result["success"] is False
    assert result["total"] == 3
    assert result["removed"] == ["sensor.a"]
    assert result["skipped"] == ["sensor.missing"]
    [error] = result["errors"]
    assert error["entity_id"] == BAD_ID
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "entity_id" in error["message"]
    assert entity_registry.async_get("sensor.a") is None
    removals = [m.args[0] for m in sent.call_args_list if m.args[0]["type"] == REMOVE]
    assert [r["entity_id"] for r in removals] == ["sensor.a", "sensor.missing", BAD_ID]


async def test_a_bulk_removal_of_every_id_succeeds(
    tools: EntityTools, entity_registry: er.EntityRegistry
) -> None:
    result = await tools.ha_remove_entity(entity_id=["sensor.a", "sensor.b"])

    assert result["success"] is True
    assert result["removed"] == ["sensor.a", "sensor.b"]
    assert result["skipped"] == []
    assert result["errors"] == []
    assert entity_registry.async_get("sensor.a") is None
    assert entity_registry.async_get("sensor.b") is None


async def test_a_single_id_removal_keeps_the_single_entity_answer(
    tools: EntityTools, entity_registry: er.EntityRegistry
) -> None:
    """A plain string is not a one-item bulk run: callers read this envelope."""
    result = await tools.ha_remove_entity(entity_id="sensor.a")

    assert result == {"success": True, "entity_id": "sensor.a"}
    assert entity_registry.async_get("sensor.a") is None


@pytest.mark.parametrize(
    ("entity_ids", "message"),
    [
        pytest.param([f"sensor.e{i}" for i in range(101)], "exceeds maximum", id="cap"),
        pytest.param(["sensor.a", "  "], "", id="blank_id"),
    ],
)
async def test_a_refused_bulk_removal_removes_nothing(
    tools: EntityTools,
    entity_registry: er.EntityRegistry,
    sent: AsyncMock,
    entity_ids: list[str],
    message: str,
) -> None:
    """The whole list is checked before the first removal goes out."""
    entity_registry.async_get_or_create(
        "sensor", "demo", "e0", suggested_object_id="e0"
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_remove_entity(entity_id=entity_ids)

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert message in error["message"]
    assert REMOVE not in sent_types(sent)
    assert entity_registry.async_get("sensor.a") is not None
    assert entity_registry.async_get("sensor.e0") is not None


async def test_an_empty_removal_list_is_refused(tools: EntityTools) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_remove_entity(entity_id=[])

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "empty" in error["message"].lower()


async def test_entity_id_with_unique_id_is_refused_before_any_read(
    tools: EntityTools, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity(entity_id="sensor.a", unique_id="sensor_a")

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "exactly one" in error["message"].lower()
    assert sent.call_count == 0


async def test_a_resolver_filter_without_unique_id_is_refused(
    tools: EntityTools,
) -> None:
    """domain narrows a unique_id lookup; on an entity_id read it would be
    silently ignored."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_entity(entity_id="sensor.a", domain="sensor")

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "resolver filter" in error["message"]
