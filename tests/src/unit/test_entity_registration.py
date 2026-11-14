"""Post-write entity lookup must wait for HA's asynchronous registration.

The lookups read a real entity registry: through the component's
``entity_lookup`` command (``component_core_client``) or, without the
component, Core's own ``config/entity_registry/*`` commands (``core_client``).
The poll's clock and sleep are this module's own, replaced so a test can
register the entity between two lookups without waiting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import entity_registration as registration
from ha_mcp.tools.component_config_reads import (
    WS_ENTITY_LOOKUP,
    fetch_entity_lookup_via_component,
)

from ._core_setup import with_config_component

KEY = "storage_key"
Domain = Literal["scene", "script"]


class PollClock:
    """The poll's monotonic clock and sleep; a sleep can run a callback."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: dict[int, Callable[[], object]] = {}

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        if callback := self.on_sleep.get(len(self.sleeps)):
            callback()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> PollClock:
    """Advance polling time without sleeping or changing the event loop clock."""
    clock = PollClock()
    monkeypatch.setattr(
        registration, "time", SimpleNamespace(monotonic=clock.monotonic)
    )
    monkeypatch.setattr(
        registration,
        "asyncio",
        SimpleNamespace(sleep=clock.sleep, timeout=asyncio.timeout),
    )
    return clock


@pytest.fixture(params=[True, False], ids=["component", "legacy"])
async def client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """The server's client, with or without the component's commands."""
    assert await async_setup_component(hass, "config", {})
    if request.param:
        websocket_api.async_register_commands(hass)
    return core_client


@pytest.fixture
async def legacy_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """No component: the lookup reads Core's entity registry commands."""
    return await with_config_component(hass, core_client)


def _platform(domain: Domain) -> str:
    return "homeassistant" if domain == "scene" else "script"


def _register(
    entity_registry: er.EntityRegistry,
    domain: Domain,
    object_id: str = "friendly_name",
    *,
    unique_id: str = KEY,
    platform: str | None = None,
) -> str:
    """Register an entity as a UI scene or script would; return its entity_id."""
    return entity_registry.async_get_or_create(
        domain,
        platform or _platform(domain),
        unique_id,
        suggested_object_id=object_id,
    ).entity_id


def _component_lookups() -> Any:
    """Spy on the component lookup where the poll calls it; it still answers."""
    return patch.object(
        registration,
        "fetch_entity_lookup_via_component",
        wraps=fetch_entity_lookup_via_component,
    )


def _ws_spy(client: HomeAssistantClient) -> Any:
    return patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    )


def _uses_component(hass: HomeAssistant) -> bool:
    return WS_ENTITY_LOOKUP in hass.data["websocket_api"]


def _legacy_reads_per_lookup(domain: Domain) -> int:
    """A script is read directly by its storage-key entity_id before the list."""
    return 2 if domain == "script" else 1


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_delayed_registration_returns_actual_entity_after_three_misses(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: HomeAssistantClient,
    clock: PollClock,
    domain: Domain,
) -> None:
    """Returning a constructed ID after one or two misses recreates #2426."""
    clock.on_sleep[3] = lambda: _register(entity_registry, domain)

    with _component_lookups() as lookup, _ws_spy(client) as send:
        result = await registration.resolve_entity_id_after_write(
            client, KEY, domain, timeout=1.0, poll_interval=0.2
        )

    assert result == f"{domain}.friendly_name"
    assert clock.now == pytest.approx(0.6)
    assert lookup.await_count == 4
    lookup.assert_awaited_with(client, KEY, domain=domain)
    if _uses_component(hass):
        send.assert_not_awaited()
    else:
        assert send.await_count == 4 * _legacy_reads_per_lookup(domain)
        send.assert_awaited_with({"type": "config/entity_registry/list"})


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_component_disappearing_during_registration_uses_legacy(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    clock: PollClock,
    monkeypatch: pytest.MonkeyPatch,
    domain: Domain,
) -> None:
    assert await async_setup_component(hass, "config", {})

    def downgrade_and_register() -> None:
        # The component no longer registers the command; Core answers
        # unknown_command, and the entity is registered by now.
        monkeypatch.delitem(hass.data["websocket_api"], WS_ENTITY_LOOKUP)
        _register(entity_registry, domain)

    clock.on_sleep[1] = downgrade_and_register

    with _ws_spy(component_core_client) as send:
        result = await registration.resolve_entity_id_after_write(
            component_core_client, KEY, domain, timeout=1.0
        )

    assert result == f"{domain}.friendly_name"
    assert clock.now == pytest.approx(0.2)
    assert send.await_count == _legacy_reads_per_lookup(domain)


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_absence_uses_whole_budget_then_constructed_fallback(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    clock: PollClock,
    domain: Domain,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        _ws_spy(client) as send,
        caplog.at_level(logging.DEBUG, logger=registration.__name__),
    ):
        result = await registration.resolve_entity_id_after_write(
            client, KEY, domain, timeout=0.55, poll_interval=0.2
        )

    assert result == f"{domain}.{KEY}"
    assert clock.now == pytest.approx(0.55)
    assert clock.sleeps == pytest.approx([0.2, 0.2, 0.15])
    assert "budget exhausted" in caplog.text
    assert f"{domain}.{KEY}" in caplog.text
    if _uses_component(hass):
        send.assert_not_awaited()


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_immediate_match_returns_without_sleep_or_extra_lookup(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: HomeAssistantClient,
    clock: PollClock,
    domain: Domain,
) -> None:
    _register(entity_registry, domain)

    with _component_lookups() as lookup, _ws_spy(client) as send:
        result = await registration.resolve_entity_id_after_write(client, KEY, domain)

    assert result == f"{domain}.friendly_name"
    assert clock.sleeps == []
    lookup.assert_awaited_once()
    assert send.await_count == (
        0 if _uses_component(hass) else _legacy_reads_per_lookup(domain)
    )


@pytest.mark.parametrize("domain", ["scene", "script"])
async def test_lookup_ignores_wrong_domain_platform_and_colliding_entity_id(
    entity_registry: er.EntityRegistry,
    client: HomeAssistantClient,
    clock: PollClock,
    domain: Domain,
) -> None:
    other_domain: Domain = "script" if domain == "scene" else "scene"
    _register(entity_registry, other_domain, "wrong_domain", platform=_platform(domain))
    _register(entity_registry, domain, "unrelated", platform="unrelated_integration")
    _register(entity_registry, domain, KEY, unique_id="other_key")
    _register(entity_registry, domain)

    result = await registration.resolve_entity_id_after_write(client, KEY, domain)

    assert result == f"{domain}.friendly_name"
    assert clock.sleeps == []


async def test_lookup_duration_consumes_registration_budget(
    component_core_client: HomeAssistantClient,
    clock: PollClock,
) -> None:
    async def slow_lookup(*args: Any, **kwargs: Any) -> Any:
        clock.now += 0.3
        return await fetch_entity_lookup_via_component(*args, **kwargs)

    with patch.object(
        registration,
        "fetch_entity_lookup_via_component",
        AsyncMock(side_effect=slow_lookup),
    ):
        result = await registration.resolve_entity_id_after_write(
            component_core_client, KEY, "scene", timeout=0.4
        )

    assert result == f"scene.{KEY}"
    assert clock.now == pytest.approx(0.4)
    assert clock.sleeps == pytest.approx([0.1])


async def test_zero_budget_performs_one_lookup(
    component_core_client: HomeAssistantClient, clock: PollClock
) -> None:
    with _component_lookups() as lookup:
        result = await registration.resolve_entity_id_after_write(
            component_core_client, KEY, "scene", timeout=0
        )

    assert result == f"scene.{KEY}"
    lookup.assert_awaited_once()
    assert clock.sleeps == []


async def test_script_registry_get_avoids_full_listing(
    entity_registry: er.EntityRegistry,
    legacy_client: HomeAssistantClient,
    clock: PollClock,
) -> None:
    _register(entity_registry, "script", KEY)

    with _ws_spy(legacy_client) as send:
        result = await registration.resolve_entity_id_after_write(
            legacy_client, KEY, "script"
        )

    assert result == f"script.{KEY}"
    send.assert_awaited_once_with(
        {"type": "config/entity_registry/get", "entity_id": f"script.{KEY}"}
    )
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "mismatch",
    [{"unique_id": "other_key"}, {"platform": "other"}],
    ids=["unique_id", "platform"],
)
async def test_script_get_collision_still_resolves_by_storage_key(
    entity_registry: er.EntityRegistry,
    legacy_client: HomeAssistantClient,
    clock: PollClock,
    mismatch: dict[str, str],
) -> None:
    _register(entity_registry, "script", KEY, **mismatch)
    _register(entity_registry, "script", "renamed")

    result = await registration.resolve_entity_id_after_write(
        legacy_client, KEY, "script"
    )

    assert result == "script.renamed"


@pytest.mark.parametrize("registered", [False, True])
async def test_caller_fallback_waits_for_registration_budget(
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    clock: PollClock,
    registered: bool,
) -> None:
    if registered:
        clock.on_sleep[3] = lambda: _register(entity_registry, "script")

    result = await registration.resolve_entity_id_after_write(
        component_core_client,
        KEY,
        "script",
        timeout=0.8,
        fallback_entity_id="script.caller_alias",
    )

    assert result == ("script.friendly_name" if registered else "script.caller_alias")
    assert clock.now == pytest.approx(0.6 if registered else 0.8)


async def test_stalled_lookup_is_bounded_and_cancelled(
    component_core_client: HomeAssistantClient,
) -> None:
    """The timeout must bound network awaits as well as polling sleeps."""
    cancelled = asyncio.Event()

    async def stall(*args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch.object(
        registration, "fetch_entity_lookup_via_component", AsyncMock(side_effect=stall)
    ):
        async with asyncio.timeout(1.0):
            result = await registration.resolve_entity_id_after_write(
                component_core_client, KEY, "scene", timeout=0.01
            )

    assert result == f"scene.{KEY}"
    assert cancelled.is_set()


async def test_cancellation_propagates(
    client: HomeAssistantClient, clock: PollClock
) -> None:
    """A cancelled caller is not turned into a fallback ID."""
    in_flight = asyncio.Event()

    async def lookup(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        return await fetch_entity_lookup_via_component(*args, **kwargs)

    with patch.object(
        registration,
        "fetch_entity_lookup_via_component",
        AsyncMock(side_effect=lookup),
    ):
        task = asyncio.ensure_future(
            registration.resolve_entity_id_after_write(client, KEY, "scene")
        )
        await in_flight.wait()
        task.cancel()

        await asyncio.wait({task})
        assert task.cancelled()


async def test_registry_rejection_returns_fallback_with_diagnostic_detail(
    core_client: HomeAssistantClient,
    clock: PollClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without Core's config API the registry read is refused: no retry."""
    result = await registration.resolve_entity_id_after_write(core_client, KEY, "scene")

    assert result == f"scene.{KEY}"
    assert clock.sleeps == []
    assert "Unknown command" in caplog.text
    assert "unknown_command" in caplog.text


async def test_lost_connection_returns_fallback_without_retry(
    legacy_client: HomeAssistantClient, clock: PollClock
) -> None:
    with patch.object(
        legacy_client,
        "send_websocket_message",
        side_effect=HomeAssistantConnectionError("offline"),
    ):
        result = await registration.resolve_entity_id_after_write(
            legacy_client, KEY, "scene"
        )

    assert result == f"scene.{KEY}"
    assert clock.sleeps == []
