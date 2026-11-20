"""``ha_config_set_helper``'s ``action`` picks create or update (#1150).

Without ``action`` a ``helper_id`` means update, so a rename (``name`` plus
``helper_id``) updates. ``action='create'`` refuses a ``helper_id`` and
``action='update'`` requires one. A ``helper_id`` no helper has, passed with a
``name``, suggests creating instead.

The tool runs on the server's client against a real ``hass`` storing Core's
input_booleans.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._helper_tool_core import create_item, fresh_name, set_up, stored_item
from ._tool_errors import rejected_call_body


@pytest.fixture(autouse=True)
async def input_booleans(hass: HomeAssistant) -> None:
    await set_up(hass, "input_boolean")


def _set(client: HomeAssistantClient, **kwargs: Any) -> Awaitable[dict[str, Any]]:
    return HelperConfigTools(client).ha_config_set_helper(
        helper_type="input_boolean", **kwargs
    )


async def _refusal(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    error: dict[str, Any] = (await rejected_call_body(_set(client, **kwargs)))["error"]
    return error


async def _stored(client: HomeAssistantClient) -> list[dict[str, Any]]:
    listed = await client.send_websocket_message({"type": "input_boolean/list"})
    assert listed["success"], listed
    items: list[dict[str, Any]] = listed["result"]
    return items


async def _existing(
    client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> tuple[str, str]:
    """A stored input_boolean: its storage id and its entity_id."""
    item_id = await create_item(client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )
    assert entity_id is not None
    return item_id, entity_id


def _hints(error: dict[str, Any]) -> str:
    return " ".join([error["message"], *error.get("suggestions", [])])


@pytest.mark.parametrize("action", [None, "create"], ids=["implicit", "explicit"])
async def test_a_name_alone_creates_a_helper(
    core_client: HomeAssistantClient, action: str | None
) -> None:
    name = fresh_name()

    result = await _set(core_client, name=name, action=action)

    assert result["action"] == "create"
    stored = await stored_item(core_client, "input_boolean", result["data"]["id"])
    assert stored["name"] == name


@pytest.mark.parametrize("action", [None, "update"], ids=["implicit", "explicit"])
async def test_a_rename_updates_the_helper_instead_of_creating_one(
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    action: str | None,
) -> None:
    """A rename passes both ``name`` and ``helper_id``; it must not add a helper."""
    item_id, entity_id = await _existing(core_client, entity_registry)
    new_name = fresh_name()

    result = await _set(core_client, name=new_name, helper_id=entity_id, action=action)

    assert result["action"] == "update"
    assert [(i["id"], i["name"]) for i in await _stored(core_client)] == [
        (item_id, new_name)
    ]


async def test_create_with_a_helper_id_is_refused_without_writing(
    core_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    """The caller cannot mean both, so neither a new helper nor a rename lands."""
    _, entity_id = await _existing(core_client, entity_registry)
    before = await _stored(core_client)

    error = await _refusal(
        core_client, name=fresh_name(), helper_id=entity_id, action="create"
    )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "contradictory" in error["message"]
    assert "Omit helper_id" in _hints(error)
    assert "action='update'" in _hints(error)
    assert await _stored(core_client) == before


async def test_update_without_a_helper_id_is_refused_without_creating(
    core_client: HomeAssistantClient,
) -> None:
    error = await _refusal(core_client, name=fresh_name(), action="update")

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "helper_id" in error["message"]
    assert "action='create'" in _hints(error)
    assert await _stored(core_client) == []


@pytest.mark.parametrize(
    ("helper_id", "action"),
    [("input_boolean.nonexistent_typo", None), ("nonexistent_typo", "update")],
    ids=["entity_id", "bare_id_explicit_update"],
)
async def test_a_misspelled_helper_id_with_a_name_suggests_creating_it(
    core_client: HomeAssistantClient, helper_id: str, action: str | None
) -> None:
    """The caller may have meant a new helper of that name; nothing is created."""
    error = await _refusal(
        core_client, name="Brand New Helper", helper_id=helper_id, action=action
    )

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "nonexistent_typo" in str(error)
    assert "omit helper_id" in _hints(error)
    assert "Brand New Helper" in _hints(error)
    assert await _stored(core_client) == []


@pytest.mark.parametrize(
    "helper_id", ["nonexistent_typo", "input_boolean.nonexistent_typo"]
)
async def test_a_misspelled_helper_id_without_a_name_does_not_suggest_creating(
    core_client: HomeAssistantClient, helper_id: str
) -> None:
    """With no name there is no helper to suggest creating."""
    error = await _refusal(core_client, helper_id=helper_id)

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "nonexistent_typo" in str(error)
    assert "If you meant to create" not in _hints(error)


@pytest.mark.parametrize("action", [None, "create"], ids=["implicit", "explicit"])
async def test_a_blank_name_is_refused_on_create(
    core_client: HomeAssistantClient, action: str | None
) -> None:
    """The blank-name refusal, not a discriminator error, whichever way create is chosen."""
    error = await _refusal(core_client, name="", action=action)

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "name cannot be blank" in error["message"]
    assert await _stored(core_client) == []
