"""The Access switch closes and reopens the in-process server in real HA (#2801).

The unit tests cover the switch, the setup gate and the teardown against
fakes. This drives the switch through Home Assistant itself: the entity the
component registers disabled, enabling it, ``switch.turn_off`` closing the
webhook, the state and the stopped server surviving an entry reload, and
``switch.turn_on`` bringing the server back.

It switches off the server of this worker's session container and turns it
back on in ``finally``, waiting for the webhook, then disables the entity
again, which removes it without a reload; xdist gives every worker its own
container, so no other test sees the gap or the enabled switch.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ..._conftest_embedded import (
    _EMBEDDED_ENTRY_ID,
    _embedded_initialize_ok,
    _wait_for_embedded_webhook_ready,
)
from ...utilities.wait_helpers import wait_for_condition

pytestmark = pytest.mark.embedded_only

_READY_TIMEOUT_S = 300


async def _access_switch_id(ha_client: Any) -> str:
    entries = await ha_client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    assert entries.get("success"), entries
    [entity_id] = [
        e["entity_id"]
        for e in entries["result"]
        if e.get("unique_id") == f"{_EMBEDDED_ENTRY_ID}_server_access"
    ]
    return str(entity_id)


async def _set_switch_enabled(ha_client: Any, entity_id: str, enabled: bool) -> None:
    result = await ha_client.send_websocket_message(
        {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "disabled_by": None if enabled else "user",
        }
    )
    assert result.get("success"), result


async def _has_state(ha_client: Any, entity_id: str) -> bool:
    return any(s["entity_id"] == entity_id for s in await ha_client.get_states())


async def _server_summary(ha_client: Any) -> dict[str, Any]:
    download = await ha_client._request(
        "GET", f"/diagnostics/config_entry/{_EMBEDDED_ENTRY_ID}"
    )
    return dict(download["data"]["server"])


@pytest.mark.timeout(900)
async def test_switching_access_off_closes_the_server_until_switched_on(
    ha_client: Any, ha_container_with_fresh_config: dict[str, Any]
) -> None:
    webhook_url = ha_container_with_fresh_config["embedded_webhook_url"]
    entity_id = await _access_switch_id(ha_client)
    # Registered disabled, so no state until a user enables it.
    assert not await _has_state(ha_client, entity_id)

    async def _switch_added() -> bool:
        # Core 2026.11 writes an "unavailable" placeholder as soon as the
        # entity is enabled; the switch itself arrives with the reload.
        states = {s["entity_id"]: s["state"] for s in await ha_client.get_states()}
        return states.get(entity_id) in ("on", "off")

    await _set_switch_enabled(ha_client, entity_id, True)
    try:
        # Home Assistant reloads the entry 30 s after the entity is enabled,
        # which adds the switch and restarts the server.
        assert await wait_for_condition(
            _switch_added, timeout=120, condition_name="access switch added"
        )
        assert await asyncio.to_thread(
            _wait_for_embedded_webhook_ready, webhook_url, _READY_TIMEOUT_S
        )

        try:
            await ha_client.call_service("switch", "turn_off", {"entity_id": entity_id})
            assert (await ha_client.get_entity_state(entity_id))["state"] == "off"
            assert not await asyncio.to_thread(_embedded_initialize_ok, webhook_url)
            # Stopped by the switch itself, before a reload could stop it.
            summary = await _server_summary(ha_client)
            assert summary["running"] is False, summary

            await ha_client.call_service(
                "homeassistant", "reload_config_entry", {"entry_id": _EMBEDDED_ENTRY_ID}
            )
            assert (await ha_client.get_entity_state(entity_id))["state"] == "off"
            # Decided by the time the reload returns; the webhook alone would
            # only show it once a wrongly started server finished its start.
            summary = await _server_summary(ha_client)
            assert summary["bring_up_in_progress"] is False, summary
            assert summary["running"] is False, summary
        finally:
            await ha_client.call_service("switch", "turn_on", {"entity_id": entity_id})
            ready = await asyncio.to_thread(
                _wait_for_embedded_webhook_ready, webhook_url, _READY_TIMEOUT_S
            )

        assert ready
        assert (await ha_client.get_entity_state(entity_id))["state"] == "on"
    finally:
        await _set_switch_enabled(ha_client, entity_id, False)
