"""``ha_list_floors_areas``, ``ha_set_area_or_floor`` and ``ha_remove_area_or_floor`` on real Core.

Every area, floor, label and sensor lives in Core's registries and state
machine; every reply and refusal the tools act on is Core's. The listing runs
without the ``ha_mcp_tools`` component, so it takes the two-call path that
``test_ha_floors_areas_component_routing.py`` compares with the component's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_areas import AreaTools, register_area_tools
from ha_mcp.utils.usage_logger import get_recent_logs

from ._command_faults import dropped_on, pooled_socket, received
from ._core_setup import with_config_component
from ._held_command import held_command
from ._tool_errors import rejected_call_body

AREA_CREATE = "config/area_registry/create"
AREA_LIST = "config/area_registry/list"

_CLEARABLE = (
    "floor_id",
    "icon",
    "picture",
    "temperature_entity_id",
    "humidity_entity_id",
)


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` with Core's registry commands (``config``)."""
    return await with_config_component(hass, core_client)


@pytest.fixture
def tools(client: HomeAssistantClient) -> AreaTools:
    return AreaTools(client)


@pytest.fixture
def home(area_registry: ar.AreaRegistry, floor_registry: fr.FloorRegistry) -> None:
    """Kitchen on the Ground floor, Garage on no floor, and Ghost on a floor
    that does not exist: Core stores a floor_id without checking it."""
    ground = floor_registry.async_create("Ground", level=0)
    area_registry.async_create(
        "Kitchen", floor_id=ground.floor_id, icon="mdi:silverware"
    )
    area_registry.async_create("Garage")
    area_registry.async_create("Ghost", floor_id="deleted_floor")


@pytest.fixture
def sensors(hass: HomeAssistant) -> None:
    """Two temperature sensors and a humidity sensor Core accepts on an area."""
    hass.states.async_set("sensor.kitchen_temp", "21", {"device_class": "temperature"})
    hass.states.async_set(
        "sensor.kitchen_temp_2", "22", {"device_class": "temperature"}
    )
    hass.states.async_set("sensor.kitchen_hum", "40", {"device_class": "humidity"})


@pytest.fixture
def furnished_kitchen(
    sensors: None, area_registry: ar.AreaRegistry, floor_registry: fr.FloorRegistry
) -> str:
    """A Kitchen with every clearable field set; returns its area_id."""
    ground = floor_registry.async_create("Ground")
    kitchen = area_registry.async_create(
        "Kitchen",
        floor_id=ground.floor_id,
        icon="mdi:silverware",
        picture="/local/kitchen.png",
        temperature_entity_id="sensor.kitchen_temp",
        humidity_entity_id="sensor.kitchen_hum",
    )
    return kitchen.id


def _stored(area_registry: ar.AreaRegistry, area_id: str) -> ar.AreaEntry:
    entry = area_registry.async_get_area(area_id)
    assert entry is not None, f"Core holds no area {area_id!r}"
    return entry


def _clearables(entry: ar.AreaEntry) -> dict[str, Any]:
    return {name: getattr(entry, name) for name in _CLEARABLE}


# --- listing --------------------------------------------------------------------


async def test_areas_are_nested_unassigned_or_orphaned(
    tools: AreaTools, home: None
) -> None:
    """An area on a floor Core no longer has must not vanish from the listing
    or pass as unassigned: it is reported apart, as orphaned."""
    result = await tools.ha_list_floors_areas()

    counts = (
        result["area_count"],
        result["unassigned_count"],
        result["orphaned_count"],
    )
    assert counts == (3, 1, 1)
    (ground,) = result["floors"]
    assert [a["area_id"] for a in ground["areas"]] == ["kitchen"]
    assert [a["area_id"] for a in result["unassigned_areas"]] == ["garage"]
    assert [a["area_id"] for a in result["orphaned_areas"]] == ["ghost"]


async def test_listing_fails_when_core_refuses_the_registry_reads(
    core_client: HomeAssistantClient,
) -> None:
    """Without Core's ``config`` commands both reads are refused: an empty
    home in reply would tell the caller there are no areas at all."""
    body = await rejected_call_body(AreaTools(core_client).ha_list_floors_areas())

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["error"]["message"] == "Failed to retrieve area or floor registry"
    assert body["areas_success"] is False


class TestListFieldsProjection:
    """``fields`` keeps only the named top-level keys, plus ``success``."""

    async def test_no_fields_returns_the_full_response(
        self, tools: AreaTools, home: None
    ) -> None:
        result = await tools.ha_list_floors_areas()

        assert set(result) == {
            "success",
            "floor_count",
            "area_count",
            "unassigned_count",
            "orphaned_count",
            "floors",
            "unassigned_areas",
            "orphaned_areas",
            "message",
        }

    async def test_fields_keep_only_the_named_keys_and_success(
        self, tools: AreaTools, home: None
    ) -> None:
        result = await tools.ha_list_floors_areas(fields=["floors"])

        assert set(result) == {"success", "floors"}
        assert result["success"] is True

    async def test_unknown_field_is_named_in_a_warning(
        self, tools: AreaTools, home: None
    ) -> None:
        result = await tools.ha_list_floors_areas(fields=["nonexistent_key"])

        assert result["success"] is True
        assert any("nonexistent_key" in w for w in result["warnings"])

    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param(123, id="not-a-list"),
            pytest.param('["', id="malformed-json"),
            pytest.param([], id="empty-list"),
        ],
    )
    async def test_unusable_fields_are_refused_by_name(
        self, tools: AreaTools, fields: Any
    ) -> None:
        body = await rejected_call_body(tools.ha_list_floors_areas(fields=fields))

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["parameter"] == "fields"


class TestListAreaFieldsProjection:
    """``area_fields`` slims the area records and nothing else."""

    @pytest.mark.parametrize("bucket", ["nested", "unassigned", "orphaned"])
    async def test_area_fields_slim_every_area_bucket(
        self, tools: AreaTools, home: None, bucket: str
    ) -> None:
        result = await tools.ha_list_floors_areas(area_fields=["area_id", "name"])

        records = {
            "nested": result["floors"][0]["areas"],
            "unassigned": result["unassigned_areas"],
            "orphaned": result["orphaned_areas"],
        }[bucket]
        assert records
        assert all(set(area) == {"area_id", "name"} for area in records)

    async def test_area_fields_leave_floor_records_and_top_level_keys_alone(
        self, tools: AreaTools, home: None
    ) -> None:
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])

        (ground,) = result["floors"]
        assert ground["name"] == "Ground"
        assert ground["level"] == 0
        assert {
            "success",
            "floor_count",
            "area_count",
            "floors",
            "unassigned_areas",
            "orphaned_areas",
        } <= set(result)

    async def test_unknown_area_field_empties_every_record_and_warns(
        self, tools: AreaTools, home: None
    ) -> None:
        """A typo in ``area_fields`` would otherwise come back as three lists
        of empty records with no hint why."""
        result = await tools.ha_list_floors_areas(area_fields=["are_id"])

        every_area = [
            *(a for floor in result["floors"] for a in floor["areas"]),
            *result["unassigned_areas"],
            *result["orphaned_areas"],
        ]
        assert len(every_area) == 3
        assert all(area == {} for area in every_area)
        assert any("are_id" in w for w in result["warnings"]), result["warnings"]


# --- remove ---------------------------------------------------------------------


async def test_removing_an_area_deletes_it_from_core(
    tools: AreaTools, area_registry: ar.AreaRegistry
) -> None:
    garage = area_registry.async_create("Garage")

    result = await tools.ha_remove_area_or_floor(kind="area", id=garage.id)

    assert (result["success"], result["kind"], result["area_id"]) == (
        True,
        "area",
        garage.id,
    )
    assert area_registry.async_get_area(garage.id) is None


async def test_removing_a_floor_deletes_it_from_core(
    tools: AreaTools, floor_registry: fr.FloorRegistry
) -> None:
    ground = floor_registry.async_create("Ground")

    result = await tools.ha_remove_area_or_floor(kind="floor", id=ground.floor_id)

    assert (result["success"], result["kind"], result["floor_id"]) == (
        True,
        "floor",
        ground.floor_id,
    )
    assert floor_registry.async_get_floor(ground.floor_id) is None


# --- set: area and floor writes -------------------------------------------------


async def test_updating_an_area_renames_it_in_core(
    tools: AreaTools, area_registry: ar.AreaRegistry
) -> None:
    kitchen = area_registry.async_create("Kitchen")

    result = await tools.ha_set_area_or_floor(kind="area", id=kitchen.id, name="K2")

    assert result["success"] is True
    assert _stored(area_registry, kitchen.id).name == "K2"


async def test_creating_a_floor_stores_its_level(
    tools: AreaTools, floor_registry: fr.FloorRegistry
) -> None:
    """Level 0 is the ground floor, not "no level"."""
    result = await tools.ha_set_area_or_floor(kind="floor", name="Ground", level=0)

    floor = floor_registry.async_get_floor(result["floor_id"])
    assert floor is not None
    assert (floor.name, floor.level) == ("Ground", 0)


async def test_updating_a_floor_changes_its_level(
    tools: AreaTools, floor_registry: fr.FloorRegistry
) -> None:
    ground = floor_registry.async_create("Ground", level=0)

    await tools.ha_set_area_or_floor(kind="floor", id=ground.floor_id, level=2)

    floor = floor_registry.async_get_floor(ground.floor_id)
    assert floor is not None
    assert floor.level == 2


@pytest.mark.parametrize(
    ("kind", "params", "refused"),
    [
        pytest.param("area", {"level": 1}, ["level"], id="level-on-area"),
        pytest.param("floor", {"floor_id": "ground"}, ["floor_id"], id="floor_id"),
        pytest.param("floor", {"picture": "/local/x.png"}, ["picture"], id="picture"),
        pytest.param("floor", {"labels": ["site_home"]}, ["labels"], id="labels"),
        pytest.param(
            "floor",
            {"temperature_entity_id": "sensor.t", "humidity_entity_id": "sensor.h"},
            ["temperature_entity_id", "humidity_entity_id"],
            id="sensors",
        ),
        # An explicit clear is still intent for a field floors do not have.
        pytest.param(
            "floor",
            {"temperature_entity_id": None},
            ["temperature_entity_id"],
            id="cleared-sensor",
        ),
    ],
)
async def test_parameter_of_the_other_kind_is_refused_not_dropped(
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    kind: str,
    params: dict[str, Any],
    refused: list[str],
) -> None:
    """Dropping it would report success for a write that left the field out."""
    body = await rejected_call_body(
        tools.ha_set_area_or_floor(kind=kind, name="Ground", **params)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["invalid_parameters"] == refused
    assert all(name in body["error"]["message"] for name in refused)
    assert not list(area_registry.async_list_areas())
    assert not list(floor_registry.async_list_floors())


# --- set: floor references ------------------------------------------------------


async def test_area_create_onto_an_unknown_floor_is_refused(
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """Core would store the dangling floor_id (issue #2159)."""
    floor_registry.async_create("Ground")

    body = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", name="Kitchen", floor_id="ghost_floor")
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["floor_id"] == "ghost_floor"
    assert not list(area_registry.async_list_areas())


async def test_area_update_onto_an_unknown_floor_is_refused(
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    ground = floor_registry.async_create("Ground")
    kitchen = area_registry.async_create("Kitchen", floor_id=ground.floor_id)

    body = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", id=kitchen.id, floor_id="ghost_floor")
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _stored(area_registry, kitchen.id).floor_id == ground.floor_id


async def test_empty_floor_id_takes_the_area_off_its_floor(
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """``""`` is the clear sentinel, not a floor to look up and reject."""
    ground = floor_registry.async_create("Ground")
    kitchen = area_registry.async_create("Kitchen", floor_id=ground.floor_id)

    result = await tools.ha_set_area_or_floor(kind="area", id=kitchen.id, floor_id="")

    assert result["success"] is True
    assert _stored(area_registry, kitchen.id).floor_id is None


async def test_area_is_created_on_an_existing_floor(
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    ground = floor_registry.async_create("Ground")

    result = await tools.ha_set_area_or_floor(
        kind="area", name="Kitchen", floor_id=ground.floor_id
    )

    assert _stored(area_registry, result["area_id"]).floor_id == ground.floor_id


# --- set: labels ----------------------------------------------------------------


async def test_unknown_label_is_refused_before_the_area_is_created(
    tools: AreaTools, area_registry: ar.AreaRegistry, label_registry: lr.LabelRegistry
) -> None:
    """Core drops a label id it does not have and still answers success."""
    label_registry.async_create("Site Home")

    body = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", name="Kitchen", labels=["ghost_label"])
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["unknown_labels"] == ["ghost_label"]
    assert not list(area_registry.async_list_areas())


async def test_creating_an_area_stores_its_labels(
    tools: AreaTools, area_registry: ar.AreaRegistry, label_registry: lr.LabelRegistry
) -> None:
    site_home = label_registry.async_create("Site Home")

    result = await tools.ha_set_area_or_floor(
        kind="area", name="Kitchen", labels=[site_home.label_id]
    )

    assert result["area"]["labels"] == [site_home.label_id]
    assert _stored(area_registry, result["area_id"]).labels == {site_home.label_id}


async def test_labels_core_echoes_need_no_reread(
    hass: HomeAssistant, tools: AreaTools, label_registry: lr.LabelRegistry
) -> None:
    site_home = label_registry.async_create("Site Home")

    with received(hass, AREA_LIST) as seen:
        await tools.ha_set_area_or_floor(
            kind="area", name="Kitchen", labels=[site_home.label_id]
        )

    assert seen[AREA_LIST] == []


async def test_labels_replace_the_areas_label_set(
    tools: AreaTools, area_registry: ar.AreaRegistry, label_registry: lr.LabelRegistry
) -> None:
    site_home = label_registry.async_create("Site Home")
    hvac = label_registry.async_create("HVAC")
    kitchen = area_registry.async_create("Kitchen", labels={site_home.label_id})

    await tools.ha_set_area_or_floor(kind="area", id=kitchen.id, labels=[hvac.label_id])

    assert _stored(area_registry, kitchen.id).labels == {hvac.label_id}


async def test_empty_labels_clear_the_areas_labels(
    tools: AreaTools, area_registry: ar.AreaRegistry, label_registry: lr.LabelRegistry
) -> None:
    site_home = label_registry.async_create("Site Home")
    kitchen = area_registry.async_create("Kitchen", labels={site_home.label_id})

    result = await tools.ha_set_area_or_floor(kind="area", id=kitchen.id, labels=[])

    assert result["success"] is True
    assert _stored(area_registry, kitchen.id).labels == set()


async def test_omitted_labels_are_left_as_they_are(
    tools: AreaTools, area_registry: ar.AreaRegistry, label_registry: lr.LabelRegistry
) -> None:
    site_home = label_registry.async_create("Site Home")
    kitchen = area_registry.async_create("Kitchen", labels={site_home.label_id})

    await tools.ha_set_area_or_floor(kind="area", id=kitchen.id, name="K2")

    stored = _stored(area_registry, kitchen.id)
    assert (stored.name, stored.labels) == ("K2", {site_home.label_id})


def _kitchen_with_site_home(tools: AreaTools) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(
        tools.ha_set_area_or_floor(kind="area", name="Kitchen", labels=["site_home"])
    )


async def test_label_deleted_during_the_create_fails_the_call_as_committed(
    hass: HomeAssistant,
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Core drops the label it no longer has and still answers success. The
    area exists by then, so the caller must fix the labels, not retry the
    create and get a second Kitchen."""
    label_registry.async_create("Site Home")

    async with held_command(hass, AREA_CREATE) as create:
        call = _kitchen_with_site_home(tools)
        await create.arrived.wait()
        label_registry.async_delete("site_home")
        create.release.set()
        body = await rejected_call_body(call)

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "requested labels" in body["error"]["message"]
    assert body["expected_labels"] == ["site_home"]
    assert body["write_committed"] is True
    assert body["area_id"] == "kitchen"
    assert any("ha_set_area_or_floor" in s for s in body["error"]["suggestions"]), body[
        "error"
    ]["suggestions"]
    assert _stored(area_registry, "kitchen").labels == set()


async def test_labels_restored_before_the_reread_count_as_written(
    hass: HomeAssistant,
    tools: AreaTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """The reply alone is not the verdict: what Core holds at the re-read is."""
    label_registry.async_create("Site Home")

    async with (
        held_command(hass, AREA_CREATE) as create,
        held_command(hass, AREA_LIST) as reread,
    ):
        call = _kitchen_with_site_home(tools)
        await create.arrived.wait()
        label_registry.async_delete("site_home")
        create.release.set()
        await reread.arrived.wait()
        restored = label_registry.async_create("Site Home")
        area_registry.async_update("kitchen", labels={restored.label_id})
        reread.release.set()
        (result,) = await asyncio.gather(call)

    assert result["success"] is True
    assert reread.calls == 1


async def test_reread_that_gets_no_answer_still_reports_the_write_committed(
    hass: HomeAssistant,
    tools: AreaTools,
    client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Reported as a failed create, the dropped re-read would invite a retry
    that duplicates the area."""
    label_registry.async_create("Site Home")
    socket = await pooled_socket(client)

    async with held_command(hass, AREA_CREATE) as create:
        with dropped_on(hass, AREA_LIST, socket):
            call = _kitchen_with_site_home(tools)
            await create.arrived.wait()
            label_registry.async_delete("site_home")
            create.release.set()
            body = await rejected_call_body(call)

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert (
        body["error"]["message"]
        == "Area write succeeded, but label verification failed"
    )
    assert body["write_committed"] is True
    assert body["area_id"] == "kitchen"
    assert body["expected_labels"] == ["site_home"]
    assert area_registry.async_get_area("kitchen") is not None


# --- set: temperature and humidity sensors (issue #2619) ------------------------


async def test_creating_an_area_stores_its_sensors(
    tools: AreaTools, area_registry: ar.AreaRegistry, sensors: None
) -> None:
    result = await tools.ha_set_area_or_floor(
        kind="area",
        name="Kitchen",
        temperature_entity_id="sensor.kitchen_temp",
        humidity_entity_id="sensor.kitchen_hum",
    )

    stored = _stored(area_registry, result["area_id"])
    assert (stored.temperature_entity_id, stored.humidity_entity_id) == (
        "sensor.kitchen_temp",
        "sensor.kitchen_hum",
    )


async def test_setting_one_sensor_leaves_the_other_alone(
    tools: AreaTools, area_registry: ar.AreaRegistry, furnished_kitchen: str
) -> None:
    await tools.ha_set_area_or_floor(
        kind="area",
        id=furnished_kitchen,
        temperature_entity_id="sensor.kitchen_temp_2",
    )

    stored = _stored(area_registry, furnished_kitchen)
    assert (stored.temperature_entity_id, stored.humidity_entity_id) == (
        "sensor.kitchen_temp_2",
        "sensor.kitchen_hum",
    )


async def test_empty_string_clears_the_sensors(
    tools: AreaTools, area_registry: ar.AreaRegistry, furnished_kitchen: str
) -> None:
    await tools.ha_set_area_or_floor(
        kind="area",
        id=furnished_kitchen,
        temperature_entity_id="",
        humidity_entity_id="",
    )

    stored = _stored(area_registry, furnished_kitchen)
    assert (stored.temperature_entity_id, stored.humidity_entity_id) == (None, None)


# --- the registered tool, as an MCP client calls it ------------------------------


@pytest.fixture
async def set_tool(client: HomeAssistantClient) -> Tool:
    """``ha_set_area_or_floor`` as registered: null and omission differ only
    at the FastMCP boundary."""
    mcp = FastMCP("test")
    register_area_tools(mcp, client)
    tool = await mcp.get_tool("ha_set_area_or_floor")
    assert tool is not None
    return tool


class TestClearableParamsThroughMcp:
    async def test_schema_advertises_null_and_omission_without_a_default(
        self, set_tool: Tool
    ) -> None:
        props = set_tool.parameters["properties"]
        for name in _CLEARABLE:
            assert "default" not in props[name], name
            assert {"type": "null"} in props[name]["anyOf"], name
            assert name not in set_tool.parameters.get("required", []), name

    async def test_omitted_params_leave_the_area_as_it_is(
        self,
        set_tool: Tool,
        area_registry: ar.AreaRegistry,
        furnished_kitchen: str,
    ) -> None:
        before = _clearables(_stored(area_registry, furnished_kitchen))

        await set_tool.run({"kind": "area", "id": furnished_kitchen, "name": "K2"})

        stored = _stored(area_registry, furnished_kitchen)
        assert stored.name == "K2"
        assert _clearables(stored) == before

    async def test_explicit_null_clears_every_clearable_param(
        self,
        set_tool: Tool,
        area_registry: ar.AreaRegistry,
        furnished_kitchen: str,
    ) -> None:
        """Some MCP clients drop an empty-string argument in transit, so null
        must clear as ``""`` does."""
        await set_tool.run(
            {"kind": "area", "id": furnished_kitchen, **dict.fromkeys(_CLEARABLE)}
        )

        stored = _clearables(_stored(area_registry, furnished_kitchen))
        assert stored == dict.fromkeys(_CLEARABLE)

    async def test_omitted_params_do_not_leak_the_sentinel_into_usage_logs(
        self, set_tool: Tool, area_registry: ar.AreaRegistry
    ) -> None:
        """log_tool_usage records the call's kwargs, and ha_report_issue returns
        those entries as structured content, so the UNSET default must serialize."""
        kitchen = area_registry.async_create("Kitchen")

        await set_tool.run({"kind": "area", "id": kitchen.id, "name": "K2"})

        entry = get_recent_logs(max_entries=1)[-1]
        assert entry["tool_name"] == "ha_set_area_or_floor"
        json.dumps(entry)
        assert "floor_id" not in entry["parameters"]
