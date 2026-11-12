"""``load_hidden_set`` on real Core: the config file, the registry and Assist.

The registry and states each test hands the resolver are Core's own replies to
the server's client, and the Assist exposure reads go to Core's
``homeassistant`` integration over the same client, so every explicit
``should_expose`` and the ``expose_new`` flag are what Core reports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    ExposedEntities,
    async_expose_entity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config
from ha_mcp.visibility.resolver import VisibilityDataUnavailable, load_hidden_set

_ASSIST_READS = {
    "homeassistant/expose_entity/list",
    "homeassistant/expose_new_entities/get",
}


@pytest.fixture(autouse=True)
async def assist_api(hass: HomeAssistant) -> None:
    """Load the integrations that answer the registry and Assist reads."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def save_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Write a config file to the data dir the resolver reads."""
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    def _save(**kwargs: Any) -> None:
        kwargs.setdefault("exclude_categories", [])
        save_visibility_config(tmp_path, VisibilityConfig(enabled=True, **kwargs))

    return _save


def _register(
    entity_registry: er.EntityRegistry,
    entity_id: str,
    entity_category: EntityCategory | None = None,
) -> None:
    domain, _, object_id = entity_id.partition(".")
    entry = entity_registry.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        entity_category=entity_category,
    )
    assert entry.entity_id == entity_id


async def _registry(client: HomeAssistantClient) -> dict[str, Any]:
    reply = await client.send_websocket_message({"type": "config/entity_registry/list"})
    assert reply["success"] is True
    return reply


def _assist_reads(send: AsyncMock) -> list[str]:
    return [
        call.args[0]["type"]
        for call in send.await_args_list
        if call.args[0]["type"] in _ASSIST_READS
    ]


def _assist_degraded(warnings: list[str]) -> bool:
    return any("Assist exposure data was unavailable" in w for w in warnings)


async def test_category_in_saved_config_hides_registry_entity(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """The saved config's category filter hides a diagnostic registry entity."""
    save_config(exclude_categories=["diagnostic"])
    _register(entity_registry, "sensor.diag", EntityCategory.DIAGNOSTIC)
    _register(entity_registry, "sensor.plain")

    hidden, warnings = await load_hidden_set(await _registry(core_client))

    assert hidden == {"sensor.diag"}
    assert warnings == []


async def test_corrupt_config_file_fails_open_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """A corrupt config hides nothing and says the filter is off."""
    (tmp_path / "entity_visibility.json").write_text("{ corrupt")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    _register(entity_registry, "sensor.diag", EntityCategory.DIAGNOSTIC)

    hidden, warnings = await load_hidden_set(await _registry(core_client))

    assert hidden == set()
    assert any("could not be loaded" in w for w in warnings)


async def test_explicitly_exposed_entity_stays_visible_when_new_entities_are_not(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """With "expose new entities" off, only an explicit expose keeps an entity."""
    save_config(respect_assist_exposure=True)
    _register(entity_registry, "light.a")
    _register(entity_registry, "light.b")
    async_expose_entity(hass, "conversation", "light.a", True)
    reply = await core_client.send_websocket_message(
        {
            "type": "homeassistant/expose_new_entities/set",
            "assistant": "conversation",
            "expose_new": False,
        }
    )
    assert reply["success"] is True

    hidden, warnings = await load_hidden_set(
        await _registry(core_client), None, core_client
    )

    assert hidden == {"light.b"}
    assert warnings == []


async def test_explicit_unexpose_hides_default_exposed_registry_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """An entity un-exposed from Assist is hidden although its domain is
    exposed by default.

    Core's ``expose_entity/list`` omits an un-exposed entity, so only the
    ``should_expose: false`` in the registry row's options says so.
    """
    save_config(respect_assist_exposure=True)
    _register(entity_registry, "light.a")
    _register(entity_registry, "light.b")
    async_expose_entity(hass, "conversation", "light.b", False)

    hidden, _ = await load_hidden_set(await _registry(core_client), None, core_client)

    assert hidden == {"light.b"}


async def test_states_only_entity_explicitly_exposed_stays_visible(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """An entity with no registry row stays visible when exposed explicitly;
    a sibling with no setting and no default exposure is hidden."""
    save_config(respect_assist_exposure=True)
    hass.states.async_set("sensor.exposed", "1")
    hass.states.async_set("sensor.plain", "1")
    async_expose_entity(hass, "conversation", "sensor.exposed", True)

    hidden, _ = await load_hidden_set(
        await _registry(core_client), await core_client.get_states(), core_client
    )

    assert hidden == {"sensor.plain"}


async def test_entity_allowlist_skips_the_assist_reads(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """An entity allowlist overrides Assist, so its data is never fetched."""
    save_config(
        exclude_categories=["diagnostic"],
        allow_entity_ids=["light.a"],
        respect_assist_exposure=True,
    )
    _register(entity_registry, "light.a", EntityCategory.DIAGNOSTIC)
    _register(entity_registry, "light.b")
    registry = await _registry(core_client)

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as send:
        hidden, warnings = await load_hidden_set(registry, None, core_client)

    assert hidden == {"light.b"}
    assert warnings == []
    send.assert_not_awaited()


async def test_area_allowlist_with_empty_registry_still_applies_assist(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """An area allowlist that cannot match an empty registry is dropped with a
    warning, and Assist exposure is fetched and applied instead."""
    save_config(allow_areas=["kitchen"], respect_assist_exposure=True)
    hass.states.async_set("sensor.exposed", "1")
    hass.states.async_set("sensor.hidden", "1")
    async_expose_entity(hass, "conversation", "sensor.exposed", True)
    registry = await _registry(core_client)
    assert registry["result"] == []

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as send:
        hidden, warnings = await load_hidden_set(
            registry, await core_client.get_states(), core_client
        )

    assert hidden == {"sensor.hidden"}
    assert sorted(_assist_reads(send)) == sorted(_ASSIST_READS)
    assert any("area/label allowlist" in w for w in warnings)


async def test_unanswered_assist_reads_skip_only_that_dimension(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """With no Assist answer, the deny list still hides and a warning says the
    Assist dimension was skipped."""
    save_config(respect_assist_exposure=True, deny_entity_ids=["light.denied"])
    _register(entity_registry, "light.a")
    registry = await _registry(core_client)

    with patch.object(
        core_client,
        "send_websocket_message",
        side_effect=HomeAssistantConnectionError("ws down"),
    ):
        hidden, warnings = await load_hidden_set(registry, None, core_client)

    assert hidden == {"light.denied"}
    assert _assist_degraded(warnings)


async def test_unanswered_assist_reads_fail_closed_in_strict_mode(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """Enforcement refuses rather than skip the Assist dimension."""
    save_config(respect_assist_exposure=True)
    _register(entity_registry, "light.a")
    registry = await _registry(core_client)

    with (
        patch.object(
            core_client,
            "send_websocket_message",
            side_effect=HomeAssistantConnectionError("ws down"),
        ),
        pytest.raises(VisibilityDataUnavailable, match="Assist exposure data"),
    ):
        await load_hidden_set(registry, None, core_client, strict=True)


async def test_strict_entity_allowlist_does_not_need_assist(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """Under an entity allowlist, strict mode neither reads Assist nor refuses
    when Assist would be unanswered."""
    save_config(allow_entity_ids=["light.a"], respect_assist_exposure=True)
    _register(entity_registry, "light.a")
    _register(entity_registry, "light.b")
    registry = await _registry(core_client)

    with patch.object(
        core_client,
        "send_websocket_message",
        side_effect=HomeAssistantConnectionError("ws down"),
    ) as send:
        hidden, warnings = await load_hidden_set(
            registry, None, core_client, strict=True
        )

    assert hidden == {"light.b"}
    assert warnings == []
    send.assert_not_awaited()


async def test_rejected_expose_new_read_skips_assist_instead_of_hiding(
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    save_config: Any,
) -> None:
    """When Core rejects the "expose new entities" read, the dimension is
    skipped rather than assuming new entities are not exposed, which would hide
    a default-exposed light with no explicit setting."""
    save_config(respect_assist_exposure=True)
    _register(entity_registry, "light.a")
    registry = await _registry(core_client)

    with patch.object(
        ExposedEntities,
        "async_get_expose_new_entities",
        side_effect=HomeAssistantError("boom"),
    ):
        hidden, warnings = await load_hidden_set(registry, None, core_client)

    assert hidden == set()
    assert _assist_degraded(warnings)
