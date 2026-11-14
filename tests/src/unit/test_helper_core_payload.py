"""Storage-helper payloads that Home Assistant validates (#2632).

Home Assistant validates each storage-helper create and update itself, so the
tool sends the caller's fields under Core's names. These tests cover what the
tool still adds: the merge with the stored item that Core's whole-item update
needs and the create defaults the tool documents. Where a zone's icon goes is
covered through both write paths in test_helper_config_fold.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers import update
from ha_mcp.tools.config_helpers.core_payload import (
    merged_update,
    storage_fields,
    with_create_defaults,
)
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._tool_errors import error_of


async def _stored_counter(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    """Create a storage counter in Core and return its id."""
    name = f"Count {uuid.uuid4().hex[:8]}"
    # ``config`` serves the entity-registry read the update resolves the id by.
    for domain in ("config", "counter"):
        assert await async_setup_component(hass, domain, {})
    created = await client.send_websocket_message(
        {"type": "counter/create", "name": name, "minimum": 0, "maximum": 10}
    )
    assert created["success"], created
    return str(created["result"]["id"])


# --- merged_update: Core's update writes the whole item ---------------------


def test_update_keeps_every_stored_field_it_does_not_change() -> None:
    stored = {"id": "t", "name": "T", "min": 0.0, "max": 50.0, "mode": "box"}
    body = merged_update("input_number", stored, None, None, {"max": 80.0})
    assert body == {"name": "T", "min": 0.0, "max": 80.0, "mode": "box"}


def test_a_field_set_to_null_is_removed_from_the_stored_item() -> None:
    """Core's update writes the whole item, so a field is cleared by leaving
    it out, as the helper's dialog drops a box that is emptied."""
    stored = {"id": "t", "name": "T", "min": 0.0, "max": 50.0, "initial": 5.0}
    body = merged_update("input_number", stored, None, None, {"initial": None})
    assert body == {"name": "T", "min": 0.0, "max": 50.0}


def test_a_null_field_is_left_out_of_a_create() -> None:
    assert with_create_defaults("input_number", {"min": 0, "initial": None}) == {
        "min": 0
    }


def test_config_reaches_core_as_written_with_name_and_icon_lifted() -> None:
    """``name`` and ``icon`` are Core fields too: ``config`` may carry them,
    and every other key is sent under the name it was given."""
    name, icon, fields = storage_fields(
        "counter", {"name": "Laps", "icon": "mdi:run", "minimum": "0"}, None, None
    )
    assert (name, icon, fields) == ("Laps", "mdi:run", {"minimum": "0"})


def test_a_list_is_not_a_storage_helpers_config() -> None:
    """A list answers a flow's forms; a storage helper has no flow."""
    with pytest.raises(ToolError) as exc_info:
        storage_fields("input_number", [{"min": 0}], "T", None)
    assert "written in one call" in error_of(exc_info)["message"]


def test_update_does_not_send_the_items_id() -> None:
    """Core's update schemas reject ``id``; the item is addressed separately."""
    body = merged_update("input_boolean", {"id": "b", "name": "B"}, "C", None, {})
    assert "id" not in body


def test_cleared_icon_is_left_out_of_the_item() -> None:
    """Core rejects an empty icon, so clearing removes the key instead."""
    stored = {"id": "b", "name": "B", "icon": "mdi:star"}
    kept = merged_update("input_boolean", stored, None, None, {})
    cleared = merged_update("input_boolean", stored, None, "", {})
    assert kept["icon"] == "mdi:star"
    assert "icon" not in cleared


def test_tag_update_does_not_echo_the_listed_name() -> None:
    """A tag's name lives in the entity registry and Core writes the name it is
    sent there: echoing the listed one would pin a never-named tag's default
    "Tag <id>" as a registry override."""
    stored = {"id": "abc", "name": "Tag abc", "description": "door"}
    kept = merged_update("tag", stored, None, None, {"description": "gate"})
    renamed = merged_update("tag", stored, "Front", None, {})
    assert kept == {"description": "gate"}
    assert renamed == {"name": "Front", "description": "door"}


# --- create defaults ---------------------------------------------------------


def test_a_given_tag_id_is_kept() -> None:
    assert with_create_defaults("tag", {"tag_id": "abc"})["tag_id"] == "abc"


async def test_update_path_sends_a_step_core_accepts(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A counter step of 0 is Core's to judge (its schema allows it), so the
    update reaches counter/update instead of being refused by the tool."""
    counter_id = await _stored_counter(hass, core_client)

    with patch.object(
        core_client,
        "send_websocket_message",
        wraps=core_client.send_websocket_message,
    ) as sent:
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="counter",
            helper_id=f"counter.{counter_id}",
            config={"step": 0},
            wait=False,
        )
    updates = [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0].get("type") == "counter/update"
    ]
    assert [message["step"] for message in updates] == [0]
    assert result["data"]["step"] == 0


async def test_item_vanishing_before_the_update_reports_like_the_read(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's not_found on <type>/update is the same missing item the stored
    read reports as CONFIG_NOT_FOUND, so the caller sees one code for it."""
    counter_id = await _stored_counter(hass, core_client)
    deletions: list[dict[str, Any]] = []
    read = update.stored_item

    async def delete_after_the_read(*args: Any) -> dict[str, Any]:
        # The tool has read the stored item; Core deletes it before the update
        # is sent, so Core itself answers the update.
        stored = await read(*args)
        deletions.append(
            await core_client.send_websocket_message(
                {"type": "counter/delete", "counter_id": counter_id}
            )
        )
        return stored

    with (
        patch.object(update, "stored_item", delete_after_the_read),
        pytest.raises(ToolError) as exc_info,
    ):
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="counter",
            helper_id=f"counter.{counter_id}",
            name="Renamed",
            wait=False,
        )
    assert [deleted["success"] for deleted in deletions] == [True]
    assert json.loads(str(exc_info.value))["error"]["code"] == "CONFIG_NOT_FOUND"
