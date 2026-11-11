"""ha_remove_helpers_integrations resolves a storage helper's target as
ha_config_set_helper and the backups do (#2632): a storage id first, else the
registry entry of the entity_id. A renamed entity can carry another helper's id as its object
id, so the storage id wins, and an entity_id the registry does not know is
never turned into a delete by its object id.

The tool runs on the server's own client against a real ``hass`` whose
input_button helpers and entity renames are Core's own.
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._client_spies import sent_messages


@pytest.fixture(autouse=True)
def _immediate_registry_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core answers at once; the retry backoff only adds time."""
    monkeypatch.setattr("ha_mcp.tools.tools_integrations._REGISTRY_RETRY_BASE_DELAY", 0)


@pytest.fixture
def mine() -> str:
    """A storage id no deleted registry entry can be restored for."""
    return f"my_button_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def other() -> str:
    return f"other_{uuid.uuid4().hex[:8]}"


async def _create_buttons(
    hass: HomeAssistant, client: HomeAssistantClient, *names: str
) -> None:
    """Stored input_buttons, each with the storage id and entity of its name."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "input_button", {})
    for name in names:
        created = await client.send_websocket_message(
            {"type": "input_button/create", "name": name}
        )
        assert created["success"], created
    await hass.async_block_till_done()


async def _rename(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, old: str, new: str
) -> None:
    entity_registry.async_update_entity(old, new_entity_id=new)
    await hass.async_block_till_done()


async def _rename_my_button(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, mine: str, other: str
) -> None:
    """my_button's entity was renamed; other's entity now holds
    input_button.my_button."""
    await _rename(hass, entity_registry, f"input_button.{mine}", "input_button.renamed")
    await _rename(
        hass, entity_registry, f"input_button.{other}", f"input_button.{mine}"
    )


def _deleted(spy: Mock) -> list[str]:
    return [
        call.args[0]["input_button_id"]
        for call in spy.call_args_list
        if call.args[0]["type"] == "input_button/delete"
    ]


async def _remove(
    client: HomeAssistantClient, target: str, wait: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=target, helper_type="input_button", confirm=True, wait=wait
    )
    return result


async def test_a_stored_id_wins_over_an_entity_with_its_text(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    """The bare id deletes that helper and names its own entity."""
    await _create_buttons(hass, core_client, mine, other)
    await _rename_my_button(hass, entity_registry, mine, other)

    with sent_messages(core_client) as sent:
        result = await _remove(core_client, mine)

    assert _deleted(sent) == [mine]
    assert result["entity_ids"] == ["input_button.renamed"]


async def test_a_stored_id_removal_waits_for_its_own_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    await _create_buttons(hass, core_client, mine, other)
    await _rename_my_button(hass, entity_registry, mine, other)
    verify = AsyncMock(return_value=True)

    with patch("ha_mcp.tools.tools_integrations.verify_entity_removed", verify):
        await _remove(core_client, mine, wait=True)

    assert verify.await_args is not None
    assert verify.await_args.args[1] == "input_button.renamed"


async def test_a_repeated_bare_id_removal_deletes_no_other_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    """After my_button's removal input_button.my_button is still other's
    entity: a repeated bare my_button would delete other."""
    await _create_buttons(hass, core_client, mine, other)
    await _rename_my_button(hass, entity_registry, mine, other)
    await _remove(core_client, mine)
    await hass.async_block_till_done()

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await _remove(core_client, mine)

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    assert _deleted(sent) == []


async def test_an_entity_id_removes_the_helper_its_entity_belongs_to(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    await _create_buttons(hass, core_client, other)
    await _rename(
        hass, entity_registry, f"input_button.{other}", f"input_button.{mine}"
    )

    with sent_messages(core_client) as sent:
        await _remove(core_client, f"input_button.{mine}")

    assert _deleted(sent) == [other]


async def test_an_unknown_entity_id_is_not_deleted_by_its_object_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
) -> None:
    """input_button.my_button names no entity, and stored my_button belongs to
    input_button.renamed: deleting by the object id would remove it."""
    await _create_buttons(hass, core_client, mine)
    await _rename(hass, entity_registry, f"input_button.{mine}", "input_button.renamed")

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await _remove(core_client, f"input_button.{mine}")

    assert json.loads(str(exc_info.value))["error"]["code"] == "ENTITY_NOT_FOUND"
    assert _deleted(sent) == []


async def test_an_unreadable_list_deletes_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without the stored ids the storage id cannot win: input_button.my_button
    would resolve to "other", a different helper. With input_button not set up,
    Core refuses the list command while the registry still holds the entities."""
    assert await async_setup_component(hass, "config", {})
    entity_registry.async_get_or_create(
        "input_button", "input_button", "other", suggested_object_id="my_button"
    )
    entity_registry.async_get_or_create(
        "input_button", "input_button", "my_button", suggested_object_id="renamed"
    )

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await _remove(core_client, "my_button")

    assert "Failed to fetch input_button config list" in str(exc_info.value)
    assert _deleted(sent) == []
