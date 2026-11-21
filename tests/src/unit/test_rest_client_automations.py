"""The client's automation config reads, writes and removals, on a real ``hass``.

Core's config API keeps the automations in automations.yaml; an entity_id is
resolved to its config id through Core's entity registry, or its state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._automation_core import entity_id_of
from ._command_faults import dropped_on, pooled_socket
from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._rest_client_config_core import (
    config_client as config_client,
)
from ._rest_client_config_core import (
    error_status,
    load,
    set_up_config_api,
    write_config_file,
)

KITCHEN_ID = "1700000000001"
HALL_ID = "1700000000002"
KITCHEN = {"id": KITCHEN_ID, "alias": "Kitchen lights", "triggers": [], "actions": []}
HALL = {"id": HALL_ID, "alias": "Hall lights", "triggers": [], "actions": []}
BODY = {"alias": "Edited", "triggers": [], "actions": []}


async def _alias(client: HomeAssistantClient, config_id: str) -> str:
    return str((await client.get_automation_config(config_id))["alias"])


# --- removal ----------------------------------------------------------------------


async def test_removing_an_automation_by_entity_id_removes_its_config(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "automation", [KITCHEN])
    entity_id = entity_id_of(hass, KITCHEN_ID)

    result = await config_client.delete_automation_config(entity_id)

    assert (result["identifier"], result["unique_id"], result["operation"]) == (
        entity_id,
        KITCHEN_ID,
        "deleted",
    )
    assert await error_status(config_client.get_automation_config(KITCHEN_ID)) == 404


async def test_removing_a_missing_automation_reports_not_found(
    config_client: HomeAssistantClient,
) -> None:
    """Core answers a removal of a missing id with a 400; passed on as one,
    the caller reads it as a malformed request."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_automation_config(HALL_ID)

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value).lower()


async def test_a_server_error_on_removal_is_not_reported_as_not_found(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await write_config_file(hass, "automations.yaml", "- [unclosed\n")

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_automation_config(KITCHEN_ID)

    assert exc_info.value.status_code == 500


async def test_a_config_id_is_removed_without_a_registry_lookup(
    hass: HomeAssistant, config_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await load(hass, "automation", [KITCHEN])

    result = await config_client.delete_automation_config(KITCHEN_ID)

    assert result["unique_id"] == KITCHEN_ID
    sent.assert_not_awaited()
    assert await error_status(config_client.get_automation_config(KITCHEN_ID)) == 404


# --- the config id in the body (#1404) ----------------------------------------------


@pytest.mark.parametrize("resolved", [False, True], ids=["entity-id", "resolved-id"])
async def test_a_body_id_of_another_automation_is_refused_and_it_is_kept(
    hass: HomeAssistant, config_client: HomeAssistantClient, resolved: bool
) -> None:
    """Core stores by the body's id: writing would overwrite the hall
    automation while reporting success for the kitchen one."""
    await load(hass, "automation", [KITCHEN, HALL])
    target = KITCHEN_ID if resolved else entity_id_of(hass, KITCHEN_ID)

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.upsert_automation_config(
            {**BODY, "id": HALL_ID}, identifier=target, _resolved=resolved
        )

    assert exc_info.value.status_code == 400
    message = str(exc_info.value)
    assert "Mismatched" in message
    assert repr(KITCHEN_ID) in message
    assert repr(HALL_ID) in message
    assert await _alias(config_client, HALL_ID) == HALL["alias"]


async def test_an_empty_body_id_is_refused(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """An id templated from an empty variable is not a missing id."""
    await load(hass, "automation", [KITCHEN])

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.upsert_automation_config(
            {**BODY, "id": ""}, identifier=entity_id_of(hass, KITCHEN_ID)
        )

    assert exc_info.value.status_code == 400
    assert await _alias(config_client, KITCHEN_ID) == KITCHEN["alias"]


async def test_the_targets_own_body_id_is_written(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "automation", [KITCHEN])

    result = await config_client.upsert_automation_config(
        {**BODY, "id": KITCHEN_ID}, identifier=entity_id_of(hass, KITCHEN_ID)
    )

    assert result["unique_id"] == KITCHEN_ID
    assert await _alias(config_client, KITCHEN_ID) == "Edited"


async def test_an_entity_id_target_is_written_under_its_config_id(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """With no id in the body, Core would store it under no id of its own."""
    await load(hass, "automation", [KITCHEN])

    result = await config_client.upsert_automation_config(
        dict(BODY), identifier=entity_id_of(hass, KITCHEN_ID)
    )

    assert result["unique_id"] == KITCHEN_ID
    stored = await config_client.get_automation_config(KITCHEN_ID)
    assert (stored["id"], stored["alias"]) == (KITCHEN_ID, "Edited")


async def test_a_creation_with_a_body_id_is_refused(
    config_client: HomeAssistantClient,
) -> None:
    """It would overwrite the automation with that id, or create one under
    an id the caller made up."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.upsert_automation_config({**BODY, "id": HALL_ID})

    assert exc_info.value.status_code == 400
    message = str(exc_info.value)
    assert "Cannot create" in message
    assert repr(HALL_ID) in message
    assert "Omit 'id'" in message
    assert await error_status(config_client.get_automation_config(HALL_ID)) == 404


async def test_a_creation_gets_a_fresh_config_id(
    config_client: HomeAssistantClient,
) -> None:
    result = await config_client.upsert_automation_config(dict(BODY))

    assert result["unique_id"].isdigit()
    assert await _alias(config_client, result["unique_id"]) == "Edited"


# --- waiting for the new entity (#2903) ---------------------------------------------


async def test_a_fresh_explicit_id_has_its_entity_found_when_asked(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """Guessing from the id never looked for a new automation's entity."""
    result = await config_client.upsert_automation_config(
        dict(BODY), "fresh_id", _resolved=True, wait_for_entity=True
    )

    assert result["entity_id"] == entity_id_of(hass, "fresh_id")
    assert "entity_not_verified" not in result


async def test_a_write_guesses_no_operation_and_waits_only_when_asked(
    config_client: HomeAssistantClient,
) -> None:
    """A second owner of ``operation`` reported a guess the tool's own
    reading could contradict; a backup restore waits for nothing."""
    result = await config_client.upsert_automation_config(dict(BODY), "fresh_id")

    assert "operation" not in result
    assert result["entity_id"] is None


async def test_an_entity_that_never_appears_is_reported_unverified(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configuration.yaml does not include automations.yaml, so the written
    automation never runs; the write still succeeded."""
    await set_up_config_api(hass, included=("script", "scene"))
    # The wait for the entity, cut from seconds to keep the test fast.
    monkeypatch.setattr(HomeAssistantClient, "POLL_BUDGET_S", 0.2)

    result = await core_client.upsert_automation_config(
        dict(BODY), wait_for_entity=True
    )

    assert result["entity_id"] is None
    assert result["entity_not_verified"] is True
    assert await _alias(core_client, result["unique_id"]) == "Edited"


async def test_no_answer_while_looking_for_the_entity_does_not_fail_the_write(
    config_client: HomeAssistantClient,
) -> None:
    """The automation is stored; an error would have the caller write it again."""
    with patch.object(
        config_client,
        "get_states",
        side_effect=HomeAssistantConnectionError("down"),
    ):
        result = await config_client.upsert_automation_config(
            dict(BODY), wait_for_entity=True
        )

    assert result["entity_not_verified"] is True
    assert await _alias(config_client, result["unique_id"]) == "Edited"


async def test_cores_rejection_text_survives_the_rewrap(
    config_client: HomeAssistantClient,
) -> None:
    """The tool leads its error with Core's text, read from the body."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.upsert_automation_config(
            {**BODY, "triggers": [{"trigger": "no_such_trigger"}]},
            "fresh_id",
            _resolved=True,
        )

    assert exc_info.value.status_code == 400
    core_message = (exc_info.value.response_data or {})["message"]
    assert core_message.startswith("Message malformed")
    assert core_message in str(exc_info.value)


# --- resolving an entity_id ---------------------------------------------------------


async def test_a_disabled_automation_resolves_through_the_registry(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A disabled automation has no state; resolving by state reported it
    missing, so it could not be read or removed."""
    await load(hass, "automation", [KITCHEN])
    entity_id = entity_id_of(hass, KITCHEN_ID)
    entity_registry.async_update_entity(
        entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None

    stored = await config_client.get_automation_config(entity_id)

    assert stored["id"] == KITCHEN_ID


async def test_the_state_id_resolves_when_the_registry_has_no_row(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """The state's ``id`` attribute is Core's own value, so it stays the
    fallback rather than a guess."""
    await write_config_file(hass, "automations.yaml", [KITCHEN])
    hass.states.async_set("automation.kitchen", "on", {"id": KITCHEN_ID})

    stored = await config_client.get_automation_config("automation.kitchen")

    assert stored["id"] == KITCHEN_ID


async def test_the_state_id_resolves_when_the_registry_gives_no_answer(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "automation", [KITCHEN])
    entity_id = entity_id_of(hass, KITCHEN_ID)
    socket = await pooled_socket(config_client)

    with dropped_on(hass, "config/entity_registry/get", socket):
        stored = await config_client.get_automation_config(entity_id)

    assert stored["id"] == KITCHEN_ID


async def test_an_unreachable_state_read_is_not_reported_as_a_missing_automation(
    unreachable_client: HomeAssistantClient,
) -> None:
    """Reported as not found, a backup skips the automation silently and an
    agent may create a duplicate of it."""
    with pytest.raises(HomeAssistantConnectionError):
        await unreachable_client.get_automation_config("automation.kitchen")


async def test_a_failed_state_read_is_not_reported_as_a_missing_automation(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
) -> None:
    hass.states.async_set("automation.kitchen", "on", {"id": KITCHEN_ID})
    gateway.outages.append(500)

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await gateway_client.get_automation_config("automation.kitchen")

    assert exc_info.value.status_code == 500
    assert gateway.requests == ["GET /api/states/automation.kitchen"]
