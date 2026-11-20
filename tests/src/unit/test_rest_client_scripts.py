"""The client's script config reads, writes and removals, on a real ``hass``.

Core's config API keeps the scripts in scripts.yaml under their storage key. A
script renamed in the UI keeps its key under a new entity_id, so the client
resolves an id through Core's entity registry first (#463).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._command_faults import dropped_on, pooled_socket
from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._rest_client_config_core import (
    config_client as config_client,
)
from ._rest_client_config_core import error_status, load, write_config_file

SEQUENCE = [{"delay": {"seconds": 1}}]
SCRIPT = {"alias": "Test script", "sequence": SEQUENCE}


async def _stored(client: HomeAssistantClient, storage_key: str) -> dict[str, Any]:
    result: dict[str, Any] = (await client.get_script_config(storage_key))["config"]
    return result


async def _renamed(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, key: str, new: str
) -> None:
    """Script ``key`` in Core, renamed in the UI to ``script.<new>``."""
    await load(hass, "script", {key: SCRIPT})
    entity_registry.async_update_entity(f"script.{key}", new_entity_id=f"script.{new}")
    await hass.async_block_till_done()


# --- removal ----------------------------------------------------------------------


async def test_removing_a_script_removes_its_config(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "script", {"test_script": SCRIPT})

    result = await config_client.delete_script_config("test_script")

    assert result["success"] is True
    assert (result["script_id"], result["operation"]) == ("test_script", "deleted")
    assert await error_status(config_client.get_script_config("test_script")) == 404


async def test_removing_a_missing_script_reports_not_found(
    config_client: HomeAssistantClient,
) -> None:
    """Core answers a removal of a missing key with a 400; passed on as one,
    the tool reports a failed request instead of a missing script."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_script_config("nonexistent_script")

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value).lower()


async def test_a_proxy_refusing_removal_explains_the_way_around_it(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
) -> None:
    """Supervisor's ingress proxy answers DELETE with 405 (#261, #414)."""
    await load(hass, "script", {"test_script": SCRIPT})
    gateway.outages.append(405)

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await gateway_client.delete_script_config("test_script")

    assert exc_info.value.status_code == 405
    message = str(exc_info.value).lower()
    for hint in ("cannot delete", "add-on", "supervisor", "yaml", "workaround"):
        assert hint in message
    assert "delete_test_script" in message
    assert gateway.requests == ["DELETE /api/config/script/config/test_script"]


async def test_a_server_error_on_removal_is_not_reported_as_not_found(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await write_config_file(hass, "scripts.yaml", "test_script: [unclosed\n")

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_script_config("test_script")

    assert exc_info.value.status_code == 500


# --- reading and writing ------------------------------------------------------------


async def test_reading_a_script_returns_cores_config(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "script", {"test_script": SCRIPT})

    result = await config_client.get_script_config("test_script")

    assert (result["success"], result["script_id"]) == (True, "test_script")
    assert result["config"]["sequence"] == SEQUENCE


async def test_reading_a_missing_script_is_not_found(
    config_client: HomeAssistantClient,
) -> None:
    assert await error_status(config_client.get_script_config("nonexistent")) == 404


async def test_a_script_with_a_sequence_is_written(
    config_client: HomeAssistantClient,
) -> None:
    result = await config_client.upsert_script_config(dict(SCRIPT), "test_script")

    assert (result["success"], result["script_id"]) == (True, "test_script")
    assert (await _stored(config_client, "test_script"))["sequence"] == SEQUENCE


async def test_a_script_from_a_blueprint_is_written_without_a_sequence(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    blueprint = (
        "blueprint:\n  name: Pause\n  domain: script\n  input: {}\n"
        "sequence:\n  - delay: 0\n"
    )
    await write_config_file(hass, "blueprints/script/pause.yaml", blueprint)
    use = {"path": "pause.yaml", "input": {}}

    result = await config_client.upsert_script_config(
        {"alias": "Paused", "use_blueprint": use}, "test_script"
    )

    assert result["script_id"] == "test_script"
    assert (await _stored(config_client, "test_script"))["use_blueprint"] == use


async def test_a_script_with_neither_sequence_nor_blueprint_is_refused(
    config_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        await config_client.upsert_script_config({"alias": "x"}, "test_script")

    assert "sequence" in str(exc_info.value)
    assert "use_blueprint" in str(exc_info.value)
    assert await error_status(config_client.get_script_config("test_script")) == 404


@pytest.mark.parametrize("identifier", ["test_script", "script.test_script"])
async def test_a_script_without_an_alias_is_named_after_its_id(
    config_client: HomeAssistantClient, identifier: str
) -> None:
    """``script.test_script``, which no entity has, is stored as ``test_script``."""
    await config_client.upsert_script_config({"sequence": SEQUENCE}, identifier)

    assert (await _stored(config_client, "test_script"))["alias"] == "test_script"


# --- a script renamed in the UI (#463) ----------------------------------------------


@pytest.mark.parametrize("identifier", ["new_name", "script.new_name"])
async def test_a_renamed_script_is_read_under_its_storage_key(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    identifier: str,
) -> None:
    await _renamed(hass, entity_registry, "old_name", "new_name")

    result = await config_client.get_script_config(identifier)

    assert result["script_id"] == "old_name"
    assert result["config"]["alias"] == SCRIPT["alias"]


async def test_a_renamed_script_is_removed_under_its_storage_key(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _renamed(hass, entity_registry, "old_name", "new_name")

    result = await config_client.delete_script_config("new_name")

    assert result["script_id"] == "old_name"
    assert await error_status(config_client.get_script_config("old_name")) == 404


async def test_a_renamed_script_is_written_under_its_storage_key(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Written under the entity_id's slug, it would be a second script."""
    await _renamed(hass, entity_registry, "old_name", "new_name")

    result = await config_client.upsert_script_config(
        {"alias": "Edited", "sequence": SEQUENCE}, "new_name"
    )

    assert result["script_id"] == "old_name"
    assert (await _stored(config_client, "old_name"))["alias"] == "Edited"


async def test_a_missing_renamed_script_names_both_ids(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _renamed(hass, entity_registry, "original_key", "renamed_script")
    await write_config_file(hass, "scripts.yaml", {})

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.get_script_config("renamed_script")

    assert exc_info.value.status_code == 404
    assert "renamed_script" in str(exc_info.value)
    assert "original_key" in str(exc_info.value)


async def test_a_script_the_registry_does_not_know_is_read_by_its_id(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await write_config_file(hass, "scripts.yaml", {"unloaded": SCRIPT})

    result = await config_client.get_script_config("unloaded")

    assert result["config"]["alias"] == SCRIPT["alias"]


async def test_no_answer_to_the_registry_lookup_is_not_guessed_past(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Guessing the storage key from the slug writes a renamed script's
    update to a new script (#1947)."""
    await _renamed(hass, entity_registry, "old_name", "new_name")
    socket = await pooled_socket(config_client)

    with (
        dropped_on(hass, "config/entity_registry/get", socket),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await config_client.upsert_script_config(dict(SCRIPT), "new_name")


async def _collided(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """``key_a`` is renamed to ``script.first``; ``key_b`` to ``script.key_a``."""
    await load(
        hass,
        "script",
        {"key_a": {**SCRIPT, "alias": "A"}, "key_b": {**SCRIPT, "alias": "B"}},
    )
    entity_registry.async_update_entity("script.key_a", new_entity_id="script.first")
    entity_registry.async_update_entity("script.key_b", new_entity_id="script.key_a")
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("identifier", "key"),
    [
        ("key_a", "key_a"),
        ("script.key_a", "key_b"),
        ("script.key_b", "key_b"),
    ],
)
async def test_a_bare_storage_key_names_its_own_script(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    identifier: str,
    key: str,
) -> None:
    """Another script's entity being ``script.key_a`` does not take key ``key_a``;
    ``script.key_b``, which no entity has, still names key ``key_b``."""
    await _collided(hass, entity_registry)

    assert (await config_client.get_script_config(identifier))["script_id"] == key


# --- a caller that resolved the storage key already (#1813, #1935) ------------------


async def test_a_resolved_storage_key_is_written_without_a_lookup(
    config_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await config_client.upsert_script_config(
        dict(SCRIPT), "renamed_script", resolved_id="storage_key"
    )

    assert result["script_id"] == "storage_key"
    sent.assert_not_awaited()
    assert (await _stored(config_client, "storage_key"))["alias"] == SCRIPT["alias"]


async def test_a_lookup_is_sent_when_no_storage_key_is_given(
    config_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await config_client.upsert_script_config(dict(SCRIPT), "renamed_script")

    sent.assert_awaited_once()


async def test_a_renamed_script_keeps_the_callers_name_as_alias(
    config_client: HomeAssistantClient,
) -> None:
    """Named after the storage key, the script would lose its new name."""
    result = await config_client.upsert_script_config(
        {"sequence": SEQUENCE}, "new_name", resolved_id="old_storage_key"
    )

    assert result["script_id"] == "old_storage_key"
    assert (await _stored(config_client, "old_storage_key"))["alias"] == "new_name"
