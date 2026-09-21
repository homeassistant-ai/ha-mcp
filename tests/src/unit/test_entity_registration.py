"""Post-write entity lookup must wait for HA's asynchronous registration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import entity_registration as registration


@pytest.fixture
def clock(monkeypatch):
    """Advance polling time without sleeping or changing the event loop clock."""
    clock = SimpleNamespace(now=0.0, sleeps=[])

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(
        registration, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(
        registration, "asyncio", SimpleNamespace(sleep=sleep, timeout=asyncio.timeout)
    )
    return clock


@pytest.fixture
def client():
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    return client


def entry(domain, entity_id=None, unique_id="storage_key"):
    return {
        "entity_id": entity_id or f"{domain}.friendly_name",
        "unique_id": unique_id,
        "platform": "homeassistant" if domain == "scene" else "script",
    }


@pytest.mark.parametrize("domain", ["scene", "script"])
@pytest.mark.parametrize("component", [True, False])
async def test_delayed_registration_returns_actual_entity_after_three_misses(
    monkeypatch, client, clock, domain, component
):
    """Returning a constructed ID after one or two misses recreates #2426."""
    rows = [[], [], [], [entry(domain)]]
    lookup = AsyncMock(side_effect=rows if component else None, return_value=None)
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    if not component:
        pending = iter(rows)

        async def registry_read(message):
            if message["type"] == "config/entity_registry/get":
                return {"success": False}
            return {"success": True, "result": next(pending)}

        client.send_websocket_message.side_effect = registry_read

    result = await registration.resolve_entity_id_after_write(
        client, "storage_key", domain, timeout=1.0, poll_interval=0.2
    )

    assert result == f"{domain}.friendly_name"
    assert clock.now == pytest.approx(0.6)
    assert lookup.await_count == 4
    lookup.assert_awaited_with(client, "storage_key", domain=domain)
    if component:
        client.send_websocket_message.assert_not_awaited()
    else:
        assert client.send_websocket_message.await_count == (
            8 if domain == "script" else 4
        )
        client.send_websocket_message.assert_awaited_with(
            {"type": "config/entity_registry/list"}
        )


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_component_disappearing_during_registration_uses_legacy(
    monkeypatch, client, clock, domain
):
    lookup = AsyncMock(side_effect=[[], None])
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    client.send_websocket_message.return_value = {
        "success": True,
        "result": [entry(domain)],
    }

    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", domain, timeout=1.0
        )
        == f"{domain}.friendly_name"
    )
    assert clock.now == pytest.approx(0.2)
    assert client.send_websocket_message.await_count == (2 if domain == "script" else 1)


@pytest.mark.parametrize("domain", ["scene", "script"])
@pytest.mark.parametrize("component", [True, False])
async def test_absence_uses_whole_budget_then_constructed_fallback(
    monkeypatch, client, clock, domain, component
):
    lookup = AsyncMock(return_value=[] if component else None)
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)

    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", domain, timeout=0.55, poll_interval=0.2
        )
        == f"{domain}.storage_key"
    )
    assert clock.now == pytest.approx(0.55)
    assert clock.sleeps == pytest.approx([0.2, 0.2, 0.15])
    if component:
        client.send_websocket_message.assert_not_awaited()


@pytest.mark.parametrize("domain", ["scene", "script"])
@pytest.mark.parametrize("component", [True, False])
async def test_immediate_match_returns_without_sleep_or_extra_lookup(
    monkeypatch, client, clock, domain, component
):
    lookup = AsyncMock(return_value=[entry(domain)] if component else None)
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    client.send_websocket_message.return_value = {
        "success": True,
        "result": [entry(domain)],
    }

    assert (
        await registration.resolve_entity_id_after_write(client, "storage_key", domain)
        == f"{domain}.friendly_name"
    )
    assert clock.sleeps == []
    lookup.assert_awaited_once()
    assert client.send_websocket_message.await_count == (
        0 if component else (2 if domain == "script" else 1)
    )


@pytest.mark.parametrize("domain", ["scene", "script"])
@pytest.mark.parametrize("component", [True, False])
async def test_lookup_ignores_wrong_domain_platform_and_colliding_entity_id(
    monkeypatch, client, clock, domain, component
):
    other_domain = "script" if domain == "scene" else "scene"
    rows = [
        entry(domain, entity_id=f"{other_domain}.wrong_domain"),
        {**entry(domain), "platform": "unrelated_integration"},
        entry(domain, entity_id=f"{domain}.storage_key", unique_id="other_key"),
        entry(domain),
    ]
    monkeypatch.setattr(
        registration,
        "fetch_entity_lookup_via_component",
        AsyncMock(return_value=rows if component else None),
    )
    client.send_websocket_message.return_value = {"success": True, "result": rows}

    assert (
        await registration.resolve_entity_id_after_write(client, "storage_key", domain)
        == f"{domain}.friendly_name"
    )


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_component_ignores_wrong_domain(monkeypatch, client, clock, domain):
    lookup = AsyncMock(side_effect=[[entry("light")], [entry(domain)]])
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)

    assert (
        await registration.resolve_entity_id_after_write(client, "storage_key", domain)
        == f"{domain}.friendly_name"
    )
    assert clock.now == pytest.approx(0.2)
    client.send_websocket_message.assert_not_awaited()


async def test_lookup_duration_consumes_registration_budget(monkeypatch, client, clock):
    async def lookup(*args, **kwargs):
        clock.now += 0.3
        return []

    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)

    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", "scene", timeout=0.4
        )
        == "scene.storage_key"
    )
    assert clock.now == pytest.approx(0.4)
    assert clock.sleeps == pytest.approx([0.1])


async def test_zero_budget_performs_one_lookup(monkeypatch, client, clock):
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)

    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", "scene", timeout=0
        )
        == "scene.storage_key"
    )
    lookup.assert_awaited_once()
    assert clock.sleeps == []


async def test_script_registry_get_avoids_full_listing(monkeypatch, client, clock):
    monkeypatch.setattr(
        registration, "fetch_entity_lookup_via_component", AsyncMock(return_value=None)
    )
    client.send_websocket_message.return_value = {
        "success": True,
        "result": entry("script", entity_id="script.storage_key"),
    }

    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", "script"
        )
        == "script.storage_key"
    )
    client.send_websocket_message.assert_awaited_once_with(
        {"type": "config/entity_registry/get", "entity_id": "script.storage_key"}
    )
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "mismatch", [{"unique_id": "other_key"}, {"platform": "other"}]
)
async def test_script_get_collision_still_resolves_by_storage_key(
    monkeypatch, client, clock, mismatch
):
    monkeypatch.setattr(
        registration, "fetch_entity_lookup_via_component", AsyncMock(return_value=None)
    )
    client.send_websocket_message.side_effect = [
        {"success": True, "result": {**entry("script"), **mismatch}},
        {"success": True, "result": [entry("script", "script.renamed")]},
    ]
    assert (
        await registration.resolve_entity_id_after_write(
            client, "storage_key", "script"
        )
        == "script.renamed"
    )


async def test_registry_rejection_keeps_diagnostic_detail(monkeypatch, client, caplog):
    monkeypatch.setattr(
        registration, "fetch_entity_lookup_via_component", AsyncMock(return_value=None)
    )
    client.send_websocket_message.return_value = {
        "success": False,
        "error": "Admin permission required",
        "error_code": "unauthorized",
    }
    assert (
        await registration.resolve_entity_id_after_write(client, "storage_key", "scene")
        == "scene.storage_key"
    )
    assert "Admin permission required" in caplog.text
    assert "unauthorized" in caplog.text


async def test_exhausted_budget_logs_fallback(monkeypatch, client, clock, caplog):
    monkeypatch.setattr(
        registration, "fetch_entity_lookup_via_component", AsyncMock(return_value=[])
    )
    with caplog.at_level("DEBUG", logger=registration.__name__):
        await registration.resolve_entity_id_after_write(
            client, "storage_key", "scene", timeout=0.4
        )
    assert "budget exhausted" in caplog.text
    assert "scene.storage_key" in caplog.text


@pytest.mark.parametrize("registered", [False, True])
async def test_caller_fallback_waits_for_registration_budget(
    monkeypatch, client, clock, registered
):
    lookup = AsyncMock(
        side_effect=[[], [], [], [entry("script")]] if registered else None,
        return_value=[],
    )
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)

    result = await registration.resolve_entity_id_after_write(
        client,
        "storage_key",
        "script",
        timeout=0.8,
        fallback_entity_id="script.caller_alias",
    )

    assert result == ("script.friendly_name" if registered else "script.caller_alias")
    assert clock.now == pytest.approx(0.6 if registered else 0.8)


@pytest.mark.parametrize("component", [True, False])
async def test_stalled_lookup_is_bounded_and_cancelled(monkeypatch, client, component):
    """The timeout must bound network awaits as well as polling sleeps."""
    cancelled = asyncio.Event()

    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    lookup = AsyncMock(side_effect=stall if component else None, return_value=None)
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    if not component:
        client.send_websocket_message.side_effect = stall

    async with asyncio.timeout(1.0):
        result = await registration.resolve_entity_id_after_write(
            client, "storage_key", "scene", timeout=0.01
        )
    assert result == "scene.storage_key"
    assert cancelled.is_set()


@pytest.mark.parametrize("component", [True, False])
@pytest.mark.parametrize("error", [TypeError, AttributeError, asyncio.CancelledError])
async def test_programming_errors_and_cancellation_propagate(
    monkeypatch, client, clock, component, error
):
    lookup = AsyncMock(side_effect=error if component else None, return_value=None)
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    if not component:
        client.send_websocket_message.side_effect = error

    with pytest.raises(error):
        await registration.resolve_entity_id_after_write(client, "storage_key", "scene")
    assert clock.sleeps == []


@pytest.mark.parametrize("component", [True, False])
@pytest.mark.parametrize(
    "error",
    [HomeAssistantAPIError, HomeAssistantAuthError, HomeAssistantConnectionError],
)
async def test_api_failure_returns_fallback_without_retry(
    monkeypatch, client, clock, component, error
):
    lookup = AsyncMock(
        side_effect=error("offline") if component else None, return_value=None
    )
    monkeypatch.setattr(registration, "fetch_entity_lookup_via_component", lookup)
    if not component:
        client.send_websocket_message.side_effect = error("offline")

    assert (
        await registration.resolve_entity_id_after_write(client, "storage_key", "scene")
        == "scene.storage_key"
    )
    assert clock.sleeps == []
