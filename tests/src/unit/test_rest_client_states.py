"""Unit tests for ``HomeAssistantClient.get_states()``'s response-shape contract.

Every ``/states`` reply Home Assistant itself produces on success is a JSON
array, so ``get_states`` hands it through unchanged. A non-list reply
(including ``_request``'s own ``{}`` fallback for an empty body) can only come
from something in front of Home Assistant and means the fetch itself failed,
not "this instance has zero entities". Callers that verify something against
the live entity set (e.g. ``bulk_selector.py``'s topology load, or
``tools_service.py``'s operations-mode group-safety check) need that
distinction to fail closed instead of treating an unverifiable batch as a
verified-safe one.

The list replies are real Core's. The others are a gateway's, answering in
front of real Core.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client


async def test_get_states_passes_through_a_real_list(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The overwhelmingly common, correct case: unchanged."""
    hass.states.async_set("light.one", "on")

    result = await core_client.get_states()

    assert [state["entity_id"] for state in result] == ["light.one"]
    assert result == [json.loads(s.as_dict_json) for s in hass.states.async_all()]


async def test_get_states_passes_through_a_genuinely_empty_list(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An empty list IS a list -- isinstance(result, list) accepts it as-is.

    The raise in ``get_states`` is about the shape being wrong, not the count
    being zero: a ``hass`` with no entities answers ``[]``.
    """
    assert hass.states.async_all() == []

    result = await core_client.get_states()

    assert result == []


async def test_get_states_raises_on_a_dict_response(
    gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """A dict-shaped body (a gateway's error envelope instead of the expected
    array) must raise, not silently become []."""
    gateway.pages = [b'{"error": "something went wrong"}']

    with pytest.raises(HomeAssistantConnectionError):
        await gateway_client.get_states()


async def test_get_states_raises_on_an_empty_body(
    gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """``_request``'s own ``{}`` fallback for an empty body must not read as
    "zero entities" either -- it is the same non-list shape, reached through
    a different path inside ``_request``."""
    gateway.pages = [b""]

    with pytest.raises(HomeAssistantConnectionError):
        await gateway_client.get_states()


_UNREADABLE_BODIES = pytest.mark.parametrize(
    "body", [b"<html>proxy login</html>", b"\x80 not utf-8"], ids=["html", "not-utf8"]
)


@_UNREADABLE_BODIES
async def test_a_read_reply_that_is_not_json_is_an_error_not_an_empty_result(
    gateway: Gateway, gateway_client: HomeAssistantClient, body: bytes
) -> None:
    """``{}`` would pass as an entity's config, e.g. in a pre-write backup."""
    gateway.pages = [body]

    with pytest.raises(HomeAssistantAPIError):
        await gateway_client.get_entity_state("light.one")

    assert gateway.requests == ["GET /api/states/light.one"]


@_UNREADABLE_BODIES
async def test_a_write_reply_that_is_not_json_counts_as_accepted(
    gateway: Gateway, gateway_client: HomeAssistantClient, body: bytes
) -> None:
    """The write may have run; reporting it as failed invites a retry that
    repeats it, so the caller gets the empty result and verifies state."""
    gateway.pages = [body]

    assert await gateway_client.fire_event("ha_mcp_probe") == {}
    assert gateway.requests == ["POST /api/events/ha_mcp_probe"]
