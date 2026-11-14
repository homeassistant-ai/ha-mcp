"""``ha_get_entity`` enrichment through the component, on a real ``hass``.

The registry record comes from Core's ``config/entity_registry/get`` (one id)
or ``get_entries`` (a list). When the component advertises ``entity_enrich``
(with ``device_registry_child_semantics``), one ``ha_mcp_tools/entity_enrich``
frame adds the area, floor and label NAMES beside the ids the record already
carries. Without it the record comes back as Core stores it, with no
enrichment keys and a warning to install or update the component.
Registries are Core's own.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_entities
from ha_mcp.tools.component_api import (
    COMPONENT_VIA_HACS,
    get_component_caps,
    invalidate_caps,
)
from ha_mcp.tools.entity_registry_reads import GET_ENTRIES_CHUNK_SIZE
from ha_mcp.tools.tools_entities import WS_ENTITY_ENRICH, EntityTools

from ._component_frames import observed, older_component
from ._core_setup import with_config_component

ENRICHMENT_KEYS = ("area", "floor", "label_names")


class Lamp(NamedTuple):
    entity_id: str
    area_id: str
    label_id: str
    area: str
    floor: str
    label: str


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return await with_config_component(hass, component_core_client)


def _lamp(hass: HomeAssistant) -> Lamp:
    """A lamp in an area on a floor, with one label."""
    suffix = uuid.uuid4().hex[:8]
    floor = fr.async_get(hass).async_create(f"Main {suffix}")
    area = ar.async_get(hass).async_create(f"Kitchen {suffix}", floor_id=floor.floor_id)
    label = lr.async_get(hass).async_create(f"Favorites {suffix}")
    registry = er.async_get(hass)
    lamp = registry.async_get_or_create("light", "test", "lamp")
    registry.async_update_entity(
        lamp.entity_id, area_id=area.id, labels={label.label_id}
    )
    return Lamp(
        lamp.entity_id, area.id, label.label_id, area.name, floor.name, label.name
    )


def _plain(hass: HomeAssistant, unique_id: str = "plain") -> str:
    return er.async_get(hass).async_get_or_create("light", "test", unique_id).entity_id


async def test_an_entity_gets_its_area_floor_and_label_names_beside_its_ids(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    lamp = _lamp(hass)

    with observed(hass, WS_ENTITY_ENRICH) as frames:
        resp = await EntityTools(client).ha_get_entity(entity_id=lamp.entity_id)

    entry = resp["entity_entry"]
    assert (entry["area_id"], entry["labels"]) == (lamp.area_id, [lamp.label_id])
    assert (entry["area"], entry["floor"], entry["label_names"]) == (
        lamp.area,
        lamp.floor,
        [lamp.label],
    )
    assert [frame["entity_ids"] for frame in frames] == [[lamp.entity_id]]


async def test_a_bulk_get_enriches_every_entity_from_one_frame(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    lamp = _lamp(hass)
    plain = _plain(hass)

    with observed(hass, WS_ENTITY_ENRICH) as frames:
        resp = await EntityTools(client).ha_get_entity(
            entity_id=[lamp.entity_id, plain]
        )

    by_id = {e["entity_id"]: e for e in resp["entity_entries"]}
    assert (by_id[lamp.entity_id]["area"], by_id[lamp.entity_id]["label_names"]) == (
        lamp.area,
        [lamp.label],
    )
    assert (by_id[plain]["area"], by_id[plain]["label_names"]) == (None, [])
    assert [set(frame["entity_ids"]) for frame in frames] == [{lamp.entity_id, plain}]


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("entity_enrich", id="no-enrich-command"),
        # Placement from a component without it can be stale for a child device.
        pytest.param("device_registry_child_semantics", id="no-child-semantics"),
    ],
)
async def test_an_older_component_leaves_the_record_as_core_stores_it(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    older_component(monkeypatch, missing)
    lamp = _lamp(hass)

    with observed(hass, WS_ENTITY_ENRICH) as frames:
        resp = await EntityTools(client).ha_get_entity(entity_id=lamp.entity_id)

    entry = resp["entity_entry"]
    assert entry["area_id"] == lamp.area_id
    assert not [key for key in ENRICHMENT_KEYS if key in entry]
    assert frames == []
    (warning,) = resp["warnings"]
    assert "too old" in warning
    assert COMPONENT_VIA_HACS in warning


async def test_a_downgraded_component_leaves_the_record_and_is_probed_again(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    lamp = _lamp(hass)
    assert await get_component_caps(client) is not None
    monkeypatch.delitem(hass.data[websocket_api.DOMAIN], WS_ENTITY_ENRICH)

    with patch.object(tools_entities, "invalidate_caps", wraps=invalidate_caps) as drop:
        resp = await EntityTools(client).ha_get_entity(entity_id=lamp.entity_id)

    entry = resp["entity_entry"]
    assert (entry["entity_id"], entry["area_id"]) == (lamp.entity_id, lamp.area_id)
    assert not [key for key in ENRICHMENT_KEYS if key in entry]
    (warning,) = resp["warnings"]
    assert COMPONENT_VIA_HACS in warning
    drop.assert_called_once_with(client)


async def test_a_bulk_get_past_the_frame_bound_is_enriched_in_bounded_frames(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """``GET_ENTRIES_CHUNK_SIZE`` bounds one frame so a long list cannot build an
    over-size WebSocket message; every id is still enriched exactly once."""
    ids = [_plain(hass, f"l{i}") for i in range(GET_ENTRIES_CHUNK_SIZE + 1)]

    with observed(hass, WS_ENTITY_ENRICH) as frames:
        resp = await EntityTools(client).ha_get_entity(entity_id=ids)

    sent = [frame["entity_ids"] for frame in frames]
    assert len(sent) > 1
    assert all(len(chunk) <= GET_ENTRIES_CHUNK_SIZE for chunk in sent)
    assert sorted(eid for chunk in sent for eid in chunk) == sorted(ids)
    assert resp["count"] == len(ids)
    assert all("area" in entry for entry in resp["entity_entries"])


async def test_a_bulk_get_within_the_frame_bound_is_one_enrichment_frame(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    ids = [_plain(hass, f"l{i}") for i in range(GET_ENTRIES_CHUNK_SIZE)]

    with observed(hass, WS_ENTITY_ENRICH) as frames:
        await EntityTools(client).ha_get_entity(entity_id=ids)

    assert [frame["entity_ids"] for frame in frames] == [ids]
