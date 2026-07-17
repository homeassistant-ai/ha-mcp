"""Cross-seam contract tests for the ``registry_lookup`` capability.

Like ``test_component_readapi_contract.py``, these wire the REAL component
function (``_do_registry_lookup``, driven against a fake hass) underneath the
mocked WS transport and then invoke the REAL server helper-delete paths — so a
vocabulary or shape drift on either side of the seam fails here rather than
shipping a component-served response the consumer mis-shapes.

Two seams are pinned against a MULTI-entity config entry (a ``utility_meter`` and
its tariff sub-entities, all bound to one ``config_entry_id``):

- The FLOW delete resolves EVERY sub-entity from ``_do_registry_lookup``'s
  ``config_entry_id`` scan (the single-valued ``_entities_by_config_entry`` index
  would silently drop members — this proves the multi-valued scan is used).
- The SIMPLE delete resolves one ``unique_id`` from ``_do_registry_lookup``'s
  ``entity_ids`` mode in EXACTLY one lookup with NO retry-loop sleep (an
  in-process read has no WS-timing race) — ``asyncio.sleep`` is patched to fail
  the test if the resolve ever sleeps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import component_api, component_registry
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeHass,
    FakeRegEntry,
    make_view,
    wsapi,
)


def _real_component_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees the real
    capability list, including ``registry_lookup``); ``registry_lookup`` runs the
    real ``_do_registry_lookup`` against ``hass`` — the seam under test is
    everything between that return value and the delete-path response.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(hass)}
        if command_type == component_registry.WS_REGISTRY_LOOKUP:
            return {
                "success": True,
                "result": wsapi._do_registry_lookup(hass, dict(kwargs)),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _multi_entity_view() -> Any:
    """A utility_meter + two tariff sub-entities, all on ``um_entry``."""
    return make_view(
        entity={
            "sensor.energy_peak": FakeRegEntry(
                "sensor.energy_peak", config_entry_id="um_entry", unique_id="p"
            ),
            "sensor.energy_offpeak": FakeRegEntry(
                "sensor.energy_offpeak", config_entry_id="um_entry", unique_id="o"
            ),
            "select.energy_tariff": FakeRegEntry(
                "select.energy_tariff", config_entry_id="um_entry", unique_id="t"
            ),
            # Noise on a different entry — must never leak into um_entry's result.
            "light.other": FakeRegEntry(
                "light.other", config_entry_id="other", unique_id="x"
            ),
        }
    )


class _ContractClient:
    """Credentialed HA client: serves the legacy single-target get + delete, and
    FAILS LOUDLY on the whole-registry list / per-id get the component replaces."""

    def __init__(self, get_result: dict[str, Any] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._get_result = get_result
        self.deleted_entries: list[str] = []
        self.delete_calls: list[dict[str, Any]] = []

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        return {"state": "on"}

    async def delete_config_entry(self, entry_id: str) -> dict[str, Any]:
        self.deleted_entries.append(entry_id)
        return {"require_restart": False}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/entity_registry/get":
            if self._get_result is None:
                raise AssertionError("per-id get should be served by the component")
            return self._get_result
        if msg_type == "config/entity_registry/list":
            raise AssertionError(
                "whole-registry list must be replaced by registry_lookup"
            )
        if isinstance(msg_type, str) and msg_type.endswith("/delete"):
            self.delete_calls.append(msg)
            return {"success": True}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


@pytest.mark.asyncio
async def test_flow_delete_finds_all_subentities_via_real_component(monkeypatch) -> None:
    """The REAL _do_registry_lookup(config_entry_id) feeds the REAL _delete_flow_helper
    every tariff sub-entity — nothing is dropped, the whole-registry list is never
    dumped."""
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: _multi_entity_view())
    ws = _real_component_ws(FakeHass())
    # Step 1 (_get_entry_id_for_flow_helper) stays legacy: one targeted get.
    client = _ContractClient(
        get_result={"success": True, "result": {"config_entry_id": "um_entry"}}
    )
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="sensor.energy_peak",
            helper_type="utility_meter",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["entry_id"] == "um_entry"
    # ALL three um_entry members resolved via the real component scan (the
    # single-valued index would have returned only the first).
    assert set(resp["entity_ids"]) == {
        "sensor.energy_peak",
        "sensor.energy_offpeak",
        "select.energy_tariff",
    }
    assert client.deleted_entries == ["um_entry"]
    lookups = [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == component_registry.WS_REGISTRY_LOOKUP
    ]
    assert len(lookups) == 1
    assert lookups[0].kwargs["config_entry_id"] == "um_entry"


@pytest.mark.asyncio
async def test_simple_delete_resolves_in_one_lookup_no_sleep(monkeypatch) -> None:
    """The REAL _do_registry_lookup(entity_ids) resolves the SIMPLE delete's
    unique_id in EXACTLY one lookup with no retry-loop sleep."""
    monkeypatch.setattr(
        wsapi,
        "_resolve_registries",
        lambda h: make_view(
            entity={
                "input_button.my_button": FakeRegEntry(
                    "input_button.my_button", unique_id="uid-real"
                )
            }
        ),
    )
    ws = _real_component_ws(FakeHass())
    # get_result=None → the client asserts if the legacy per-id get is used at all.
    client = _ContractClient()
    tools = IntegrationTools(client)

    with (
        patch_ws(ws, component_registry),
        patch(
            "ha_mcp.tools.tools_integrations.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock,
    ):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert resp["success"] is True
    assert resp["unique_id"] == "uid-real"
    assert client.delete_calls[0]["input_button_id"] == "uid-real"
    lookups = [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == component_registry.WS_REGISTRY_LOOKUP
    ]
    assert len(lookups) == 1
    assert lookups[0].kwargs["entity_ids"] == ["input_button.my_button"]
    # In-process read → no backoff sleep during resolve.
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_delete_missing_entity_via_real_component(monkeypatch) -> None:
    """A SIMPLE target absent from the real registry resolves to no unique_id via
    the real component (``missing`` list), then the direct-id fallback deletes it —
    proving the ``entity_ids`` miss shape drives the fallback, not an error."""
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view(entity={}))
    ws = _real_component_ws(FakeHass())
    client = _ContractClient()
    tools = IntegrationTools(client)

    with patch_ws(ws, component_registry):
        resp = await tools.ha_remove_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    # Component authoritatively reported the id as missing → direct-id fallback.
    assert resp["success"] is True
    assert resp["fallback_used"] == "direct_id"
    assert client.delete_calls[0]["input_button_id"] == "my_button"
