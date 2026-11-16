"""``ha_config_set_label(areas=...)`` on real Core (issue #2455).

Agents create a label and stick it on rooms in one call. Assignment is
additive (an area keeps its other labels); replacing the set is
``ha_set_area_or_floor(kind="area", labels=...)``. Every registry row, reply
and refusal here is Core's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.config import label_registry as label_commands
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools.tools_labels import LabelTools

from ._command_faults import pooled_socket, received, unanswered
from ._held_command import held_command
from ._tool_errors import rejected_call_body
from ._ws_transport import abort_transport

AREA_UPDATE = "config/area_registry/update"
AREA_LIST = "config/area_registry/list"


@pytest.fixture
async def config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands, which hold the registries' WebSocket API."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def labels(config_commands: None, core_client: HomeAssistantClient) -> LabelTools:
    return LabelTools(core_client)


@asynccontextmanager
async def update_held_for(
    hass: HomeAssistant, area_id: str
) -> AsyncIterator[asyncio.Event]:
    """Core holds the area update of ``area_id`` until the block ends, then
    answers it; the yielded event is set once that update has arrived."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][AREA_UPDATE]
    arrived, release = asyncio.Event(), asyncio.Event()

    async def hold(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        if msg["area_id"] == area_id:
            arrived.set()
            await release.wait()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, AREA_UPDATE, websocket_api.async_response(hold), schema
    )
    try:
        yield arrived
    finally:
        release.set()
        await hass.async_block_till_done()
        websocket_api.async_register_command(hass, AREA_UPDATE, core_handler, schema)


async def test_create_with_areas_adds_the_label_and_keeps_existing_ones(
    labels: LabelTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    existing = label_registry.async_create("Existing")
    kitchen = area_registry.async_create("Kitchen", labels={existing.label_id})

    result = await labels.ha_config_set_label(name="Site Home", areas=[kitchen.id])

    assert result["label_id"] == "site_home"
    assert label_registry.async_get_label("site_home") is not None
    assert result["assigned_areas"] == [kitchen.id]
    stored = area_registry.async_get_area(kitchen.id)
    assert stored is not None
    assert stored.labels == {existing.label_id, "site_home"}


async def test_unknown_area_is_refused_before_the_label_is_created(
    labels: LabelTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """A typo in one area id leaves no half-done label behind, and the advice
    does not mention an ``area_id`` parameter this tool does not have."""
    area_registry.async_create("Kitchen")

    error = await rejected_call_body(
        labels.ha_config_set_label(name="Site Home", areas=["ghost_room"])
    )

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["unknown_area_ids"] == ["ghost_room"]
    assert error["areas"] == ["ghost_room"]
    assert all("area_id=" not in s for s in error["error"]["suggestions"])
    assert not label_registry.labels


async def test_every_unknown_area_is_reported_in_one_refusal(
    labels: LabelTools, area_registry: ar.AreaRegistry
) -> None:
    kitchen = area_registry.async_create("Kitchen")

    error = await rejected_call_body(
        labels.ha_config_set_label(
            name="Site Home", areas=["ghost_room", kitchen.id, "phantom"]
        )
    )

    assert error["unknown_area_ids"] == ["ghost_room", "phantom"]


async def test_area_registry_refusal_keeps_cores_reason(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
) -> None:
    """Without Core's own message a refused area read is indistinguishable
    from a protocol error, and the caller retries the wrong thing."""
    label_commands.async_setup(hass)

    error = await rejected_call_body(
        LabelTools(core_client).ha_config_set_label(name="Site Home", areas=["hall"])
    )

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unknown command." in error["error"]["details"]
    assert not label_registry.labels


async def test_empty_area_id_is_refused_before_anything_is_sent(
    labels: LabelTools, sent: Any
) -> None:
    error = await rejected_call_body(labels.ha_config_set_label(name="X", areas=[""]))

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_called()


async def test_area_that_already_has_the_label_is_not_rewritten(
    hass: HomeAssistant,
    labels: LabelTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    label = label_registry.async_create("Site Home")
    kitchen = area_registry.async_create("Kitchen", labels={label.label_id})

    with received(hass, AREA_UPDATE) as seen:
        result = await labels.ha_config_set_label(
            name="Site Home", label_id=label.label_id, areas=[kitchen.id]
        )

    assert result["assigned_areas"] == [kitchen.id]
    assert seen[AREA_UPDATE] == []


async def test_omitted_areas_leave_the_area_registry_alone(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    with received(hass, AREA_LIST, AREA_UPDATE) as seen:
        result = await labels.ha_config_set_label(name="Site Home")

    assert "assigned_areas" not in result
    assert seen == {AREA_LIST: [], AREA_UPDATE: []}


async def test_empty_areas_list_assigns_nothing(
    hass: HomeAssistant, labels: LabelTools
) -> None:
    with received(hass, AREA_LIST, AREA_UPDATE) as seen:
        result = await labels.ha_config_set_label(name="Site Home", areas=[])

    assert result["assigned_areas"] == []
    assert seen == {AREA_LIST: [], AREA_UPDATE: []}


async def test_several_areas_are_validated_with_one_registry_read(
    labels: LabelTools, area_registry: ar.AreaRegistry, sent: Any
) -> None:
    """Validation does not cost one round trip per area."""
    kitchen = area_registry.async_create("Kitchen")
    living = area_registry.async_create("Living")

    result = await labels.ha_config_set_label(
        name="Site Home", areas=[kitchen.id, living.id]
    )

    assert result["assigned_areas"] == [kitchen.id, living.id]
    types = [call.args[0]["type"] for call in sent.call_args_list]
    create = types.index("config/label_registry/create")
    assert types[:create].count(AREA_LIST) == 1


async def test_refused_area_update_reports_the_areas_already_done(
    hass: HomeAssistant, labels: LabelTools, area_registry: ar.AreaRegistry
) -> None:
    """The label and the first area are written when Core refuses the second
    update; the error says so, so the caller does not redo them."""
    kitchen = area_registry.async_create("Kitchen")
    living = area_registry.async_create("Living")

    async with update_held_for(hass, living.id) as arrived:
        call = asyncio.create_task(
            labels.ha_config_set_label(name="Site Home", areas=[kitchen.id, living.id])
        )
        await arrived.wait()
        area_registry.async_delete(living.id)
    error = await rejected_call_body(call)

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert error["partial"] is True
    assert error["assigned_areas"] == [kitchen.id]
    assert error["label_id"] == "site_home"
    assert error["area_id"] == living.id


async def test_dropped_connection_during_assign_stays_a_connection_error(
    hass: HomeAssistant,
    labels: LabelTools,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
) -> None:
    """Reported as a refusal, a dropped connection would hide the one recovery
    that helps; the label is written, so retrying must not recreate it."""
    kitchen = area_registry.async_create("Kitchen")
    living = area_registry.async_create("Living")

    async with update_held_for(hass, living.id) as arrived:
        call = asyncio.create_task(
            labels.ha_config_set_label(name="Site Home", areas=[kitchen.id, living.id])
        )
        await arrived.wait()
        abort_transport(await pooled_socket(core_client))
        error = await rejected_call_body(call)

    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert error["partial"] is True
    assert error["assigned_areas"] == [kitchen.id]
    assert error["label_id"] == "site_home"
    assert error["area_id"] == living.id
    assert any("do not recreate the label" in s for s in error["error"]["suggestions"])


async def test_unanswered_area_update_stays_a_timeout(
    hass: HomeAssistant,
    labels: LabelTools,
    area_registry: ar.AreaRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kitchen = area_registry.async_create("Kitchen")

    async with unanswered(hass, AREA_UPDATE, monkeypatch):
        error = await rejected_call_body(
            labels.ha_config_set_label(name="Site Home", areas=[kitchen.id])
        )

    assert error["error"]["code"] == "CONNECTION_TIMEOUT"
    assert error["partial"] is True
    assert error["assigned_areas"] == []


async def test_cancelled_call_during_assign_is_not_swallowed(
    hass: HomeAssistant, labels: LabelTools, area_registry: ar.AreaRegistry
) -> None:
    kitchen = area_registry.async_create("Kitchen")

    async with held_command(hass, AREA_UPDATE) as held:
        call = asyncio.create_task(
            labels.ha_config_set_label(name="Site Home", areas=[kitchen.id])
        )
        await held.arrived.wait()
        call.cancel()
        await asyncio.wait([call])
        assert call.cancelled()


async def test_label_added_to_a_later_area_meanwhile_is_kept(
    hass: HomeAssistant,
    labels: LabelTools,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Each area is read again right before its update, so a label another
    client adds while the call runs is not overwritten."""
    kitchen = area_registry.async_create("Kitchen")
    living = area_registry.async_create("Living")
    late = label_registry.async_create("Late")

    async with update_held_for(hass, kitchen.id) as arrived:
        call = asyncio.create_task(
            labels.ha_config_set_label(name="Site Home", areas=[kitchen.id, living.id])
        )
        await arrived.wait()
        area_registry.async_update(living.id, labels={late.label_id})
    await asyncio.wait([call])
    call.result()

    stored = area_registry.async_get_area(living.id)
    assert stored is not None
    assert stored.labels == {late.label_id, "site_home"}


async def test_target_areas_are_snapshotted_before_the_assign(
    labels: LabelTools,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label restore cannot undo area assignments, so each area is backed
    up before the label lands on it."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    reset_global_settings()
    kitchen = area_registry.async_create("Kitchen")

    await labels.ha_config_set_label(name="Site Home", areas=[kitchen.id])

    manager = get_backup_manager(core_client, get_global_settings())
    (snapshot,) = manager.list_snapshots(domain="area_or_floor")
    assert snapshot["entity_id"] == f"area:{kitchen.id}"
    assert manager.read_snapshot(snapshot["name"])["config"]["labels"] == []
