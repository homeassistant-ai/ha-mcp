"""Unit tests for the client's transient-gateway retry.

A reverse proxy / Supervisor ingress returns 502/503/504 when HA Core is
restarting or briefly overloaded behind it, and a 502 storm once failed ~190
unrelated tests at once. Reads retry with bounded backoff.

Writes never do. None of those statuses proves the request failed to execute
(RFC 9110 has 502 as "received an invalid response from an inbound server"), so
a replay can double-apply — see ``test_no_write_is_ever_gateway_retried``.

The client talks to a real Core through such a gateway: it answers the
statuses a test queues itself and forwards every other request, the
WebSocket included, to Core.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client

# An outage longer than any retry budget: the gateway is still failing when
# the client gives up.
LONG_OUTAGE = 10


def _backoffs(caplog: pytest.LogCaptureFixture) -> int:
    """How many retry backoffs the client logged before sleeping."""
    return sum(
        "retrying in" in record.getMessage()
        for record in caplog.records
        if record.name == "ha_mcp.client.rest_client"
    )


@pytest.fixture(autouse=True)
def _client_warnings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ha_mcp.client.rest_client")


@pytest.mark.asyncio
async def test_raw_request_retries_transient_502_then_succeeds(
    hass: HomeAssistant,
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass.states.async_set("light.kitchen", "on")
    gateway.outages = [502]
    states = await gateway_client.get_states()
    assert "light.kitchen" in [state["entity_id"] for state in states]
    assert gateway.requests == ["GET /api/states"] * 2
    assert _backoffs(caplog) == 1


@pytest.mark.asyncio
async def test_raw_request_exhausts_retries_then_raises(
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway.outages = [503] * LONG_OUTAGE
    with pytest.raises(HomeAssistantAPIError) as exc:
        await gateway_client.get_states()
    assert exc.value.status_code == 503
    # The retries are bounded: the client stops while the outage lasts, and
    # backs off before every attempt but the first.
    assert 1 < len(gateway.requests) < LONG_OUTAGE
    assert _backoffs(caplog) == len(gateway.requests) - 1


@pytest.mark.asyncio
async def test_raw_request_does_not_retry_non_gateway_4xx(
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Core's own 404 for an entity it does not hold, forwarded by the gateway.
    with pytest.raises(HomeAssistantAPIError) as exc:
        await gateway_client.get_entity_state("light.missing")
    assert exc.value.status_code == 404
    assert len(gateway.requests) == 1
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
async def test_raw_request_success_first_try_no_retry(
    hass: HomeAssistant,
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = await gateway_client.get_config()
    assert config["location_name"] == hass.config.location_name
    assert gateway.requests == ["GET /api/config"]
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_reads_still_retry_every_gateway_status(
    hass: HomeAssistant,
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    status: int,
) -> None:
    """Replaying a read costs nothing, so the storm protection stays."""
    hass.states.async_set("light.kitchen", "on")
    gateway.outages = [status]
    states = await gateway_client.get_states()
    assert "light.kitchen" in [state["entity_id"] for state in states]
    assert len(gateway.requests) == 2


# Every write method the client sends: POST and DELETE.
_WRITES: dict[str, Callable[[HomeAssistantClient], Awaitable[Any]]] = {
    "POST": lambda client: client.fire_event("ha_mcp_probe"),
    "DELETE": lambda client: client.abort_config_flow("flow-1"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("method", sorted(_WRITES))
async def test_no_write_is_ever_gateway_retried(
    gateway_client: HomeAssistantClient,
    gateway: Gateway,
    caplog: pytest.LogCaptureFixture,
    status: int,
    method: str,
) -> None:
    """None of these statuses proves Home Assistant did not execute the write.

    #1623 retried them on the premise that a gateway 5xx means the request
    never reached the backend. It does not: a replay can fire an event twice,
    run a script twice, or turn a completed DELETE into a misleading 404.
    """
    gateway.outages = [status] * LONG_OUTAGE
    with pytest.raises(HomeAssistantAPIError) as exc:
        await _WRITES[method](gateway_client)
    assert exc.value.status_code == status
    assert len(gateway.requests) == 1
    assert gateway.requests[0].startswith(f"{method} ")
    assert _backoffs(caplog) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_the_config_flow_submit_is_not_replayed(
    gateway_client: HomeAssistantClient, gateway: Gateway, status: int
) -> None:
    """The call that motivated the rule.

    HA consumes the flow_id on success, so a replay returns 404 "Invalid flow
    specified" — a definitive-looking 4xx that hides a first attempt which may
    already have committed.
    """
    gateway.outages = [status] * LONG_OUTAGE
    with pytest.raises(HomeAssistantAPIError):
        await gateway_client.submit_config_flow_step("flow-1", {"host": "192.0.2.1"})
    assert len(gateway.requests) == 1
