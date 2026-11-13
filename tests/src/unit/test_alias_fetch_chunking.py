"""Alias enrichment of the entity search reads Core's registry in chunks (#1721).

Aliases live only in ``config/entity_registry/get_entries``, whose reply
carries the full extended entry per entity. One request for every survivor
scaled that single reply frame with the whole instance; on a ~6.4k-entity
instance it exceeded the WebSocket message cap and killed the connection.
These tests run ``smart_entity_search`` against Core's registry: bounded
requests that still cover every entity, and enrichment that stays
best-effort when Core gives no answer or refuses the command.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.smart_search import SmartSearchTools

GET_ENTRIES = "config/entity_registry/get_entries"
SEARCH_LOGGER = "ha_mcp.tools.smart_search._entities"


def _add_lights(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, count: int
) -> list[str]:
    """``count`` lights in Core's registry and state machine, each with an alias."""
    entity_ids = []
    for index in range(count):
        entry = entity_registry.async_get_or_create(
            "light", "b11", f"lamp-{index}", suggested_object_id=f"lamp_{index}"
        )
        entity_registry.async_update_entity(
            entry.entity_id, aliases=[f"nickname{index:04d}"]
        )
        hass.states.async_set(entry.entity_id, "off")
        entity_ids.append(entry.entity_id)
    return entity_ids


def _alias_requests(spy: AsyncMock) -> list[list[str]]:
    return [
        list(args[0]["entity_ids"])
        for args, _kwargs in spy.await_args_list
        if args[0]["type"] == GET_ENTRIES
    ]


async def _search(client: HomeAssistantClient, query: str, **kwargs: Any) -> Any:
    return await SmartSearchTools(
        client=client, fuzzy_threshold=60
    ).smart_entity_search(query, limit=5, **kwargs)


async def test_a_large_registry_reads_aliases_in_bounded_requests(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """Every entity's alias arrives, through requests of one bounded size.

    The instance is larger than one request may cover, so the ids are split;
    every request but the last has the same size, which does not grow with
    the instance, and every id is asked for exactly once.
    """
    assert await async_setup_component(hass, "config", {})
    entity_ids = _add_lights(hass, entity_registry, 1201)

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        first = await _search(core_client, "nickname0000")
    last = await _search(core_client, f"nickname{len(entity_ids) - 1:04d}")

    requests = _alias_requests(spy)
    assert len(requests) > 1
    bound = len(requests[0])
    assert all(len(request) == bound for request in requests[:-1])
    assert len(requests[-1]) <= bound
    asked = [entity_id for request in requests for entity_id in request]
    assert sorted(asked) == sorted(entity_ids)
    assert "warnings" not in first
    for result, entity_id in ((first, entity_ids[0]), (last, entity_ids[-1])):
        top = result["matches"][0]
        assert (top["entity_id"], top["match_type"]) == (entity_id, "alias_match")


async def test_a_small_registry_reads_aliases_in_one_request(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """A survivor set below the bound costs exactly one request."""
    assert await async_setup_component(hass, "config", {})
    entity_ids = _add_lights(hass, entity_registry, 3)

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        result = await _search(core_client, "nickname0001")

    assert [sorted(request) for request in _alias_requests(spy)] == [sorted(entity_ids)]
    assert result["matches"][0]["entity_id"] == entity_ids[1]


async def test_no_survivors_sends_no_alias_request(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """A domain filter that keeps nothing costs no alias request."""
    assert await async_setup_component(hass, "config", {})
    _add_lights(hass, entity_registry, 3)

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        result = await _search(core_client, "nickname0001", domain_filter="switch")

    assert _alias_requests(spy) == []
    assert result["matches"] == []


async def test_no_answer_to_the_alias_requests_degrades_to_a_warning(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost connection during enrichment keeps the search, minus aliases.

    States and the registry list come from Core first; then the WebSocket
    gives no answer, so only the alias requests meet it.
    """
    assert await async_setup_component(hass, "config", {})
    entity_ids = _add_lights(hass, entity_registry, 3)
    states = await core_client.get_states()
    registry = await core_client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )

    with (
        patch.object(
            core_client,
            "send_websocket_message",
            side_effect=HomeAssistantConnectionError("connection dropped"),
        ) as dropped,
        caplog.at_level(logging.WARNING, logger=SEARCH_LOGGER),
    ):
        result = await _search(
            core_client,
            "lamp 1",
            prefetched_states=states,
            prefetched_registry=registry,
        )

    assert dropped.await_count >= 1
    assert result["success"] is True
    assert result["matches"][0]["entity_id"] == entity_ids[1]
    assert any("Alias enrichment incomplete" in w for w in result["warnings"])
    assert "alias_enrichment_failed" in caplog.text


async def test_one_unanswered_chunk_keeps_the_other_chunks_aliases(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias request that gets no answer loses only its own chunk's aliases.

    Core holds one request whose chunk lacks the searched entity until the
    client's wait for it has run out; every other request is answered at once.
    """
    assert await async_setup_component(hass, "config", {})
    entity_ids = _add_lights(hass, entity_registry, 1201)
    target = entity_ids[0]
    held = asyncio.Event()
    release = asyncio.Event()
    answered = asyncio.Event()
    core_handler, schema = hass.data[websocket_api.DOMAIN][GET_ENTRIES]

    async def hold_one_other_chunk(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        if target not in msg["entity_ids"] and not held.is_set():
            held.set()
            await release.wait()
            core_handler(core, connection, msg)
            answered.set()
        else:
            core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, GET_ENTRIES, websocket_api.async_response(hold_one_other_chunk), schema
    )
    # One real command opens the client's socket under the normal wait; the
    # wait is then cut so only the held request runs out of it.
    await core_client.send_websocket_message({"type": "config/entity_registry/list"})
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1.0)
    try:
        result = await _search(core_client, "nickname0000")
    finally:
        # Answer the held request while the client's socket is still open.
        release.set()
        if held.is_set():
            async with asyncio.timeout(5):
                await answered.wait()
            # Core writes replies in order: the pong means the held reply is out.
            await core_client.send_websocket_message({"type": "ping"})

    assert held.is_set()
    top = result["matches"][0]
    assert (top["entity_id"], top["match_type"]) == (target, "alias_match")
    assert any("Alias enrichment incomplete" in w for w in result["warnings"])


async def test_core_refusing_the_alias_request_degrades_to_a_warning(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without the ``config`` integration Core rejects ``get_entries``.

    The rejection is Core's own non-success reply; the search still answers
    from the states and says the alias part is missing.
    """
    entity_ids = _add_lights(hass, entity_registry, 3)

    with caplog.at_level(logging.WARNING, logger=SEARCH_LOGGER):
        result = await _search(core_client, "lamp 1")

    assert result["success"] is True
    assert result["matches"][0]["entity_id"] == entity_ids[1]
    assert any("Alias enrichment incomplete" in w for w in result["warnings"])
    assert "alias_enrichment_failed" in caplog.text


async def test_computed_name_alias_does_not_break_alias_search(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """Core sends the COMPUTED_NAME alias sentinel as null over the WebSocket.

    ``get_entries`` then carries ``aliases: [null, ...]``. Ingesting the null
    would put None into every downstream alias string op; only strings may
    enter the alias map, so the real alias still matches.
    """
    assert await async_setup_component(hass, "config", {})
    entry = entity_registry.async_get_or_create(
        "light", "b11", "porch", suggested_object_id="porch"
    )
    entity_registry.async_update_entity(
        entry.entity_id, aliases=[er.COMPUTED_NAME, "Lantern"]
    )
    hass.states.async_set(entry.entity_id, "off")

    result = await _search(core_client, "lantern")

    top = result["matches"][0]
    assert (top["entity_id"], top["match_type"]) == (entry.entity_id, "alias_match")
