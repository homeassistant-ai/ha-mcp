"""The visibility resolver over Core's own registry, device and state replies.

``hidden_entity_ids`` turns what Core answers the server's client
(``config/entity_registry/list``, ``config/device_registry/list``, the states)
into the set the filter hides; ``load_hidden_set`` adds Core's Assist
exposure. Entities, devices, areas and labels are Core's, created through its
registries; exposure goes through Core's ``homeassistant`` integration.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config
from ha_mcp.visibility.resolver import (
    VisibilityDataUnavailable,
    config_has_active_hide_dimensions,
    hidden_entity_ids,
    load_hidden_set,
    visibility_filter_active,
)

from .._visibility_config import visibility_config as _cfg


class Core:
    """Core's registries on one ``hass``, and its replies to the server's client."""

    def __init__(self, hass: HomeAssistant, client: HomeAssistantClient) -> None:
        self.hass = hass
        self.client = client
        self.entry = MockConfigEntry(domain="test")
        self.entry.add_to_hass(hass)

    def _area(self, area: str | None) -> str | None:
        if area is None:
            return None
        areas = ar.async_get(self.hass)
        found = areas.async_get_area(area) or areas.async_create(area.title())
        return found.id

    def _labels(self, labels: tuple[str, ...]) -> set[str]:
        registry = lr.async_get(self.hass)
        for label in labels:
            if registry.async_get_label(label) is None:
                registry.async_create(label)
        return set(labels)

    def device(
        self,
        name: str,
        *,
        area: str | None = None,
        labels: tuple[str, ...] = (),
        parent: str | None = None,
    ) -> str:
        devices = dr.async_get(self.hass)
        if parent is None:
            device_id = devices.async_get_or_create(
                config_entry_id=self.entry.entry_id, identifiers={("test", name)}
            ).id
        else:
            device_id = devices.async_get_or_create_child(
                config_entry_id=self.entry.entry_id,
                identifiers={("test", name)},
                parent_device_id=parent,
            ).id
        if area or labels:
            devices.async_update_device(
                device_id, area_id=self._area(area), labels=self._labels(labels)
            )
        return device_id

    def entity(
        self,
        entity_id: str,
        *,
        category: EntityCategory | None = None,
        hidden: bool = False,
        area: str | None = None,
        labels: tuple[str, ...] = (),
        device_id: str | None = None,
        device_class: str | None = None,
    ) -> None:
        domain, _, object_id = entity_id.partition(".")
        entities = er.async_get(self.hass)
        row = entities.async_get_or_create(
            domain,
            "test",
            object_id,
            suggested_object_id=object_id,
            config_entry=self.entry,
            device_id=device_id,
            entity_category=category,
            original_device_class=device_class,
            original_name=object_id,
        )
        assert row.entity_id == entity_id
        entities.async_update_entity(
            entity_id,
            area_id=self._area(area),
            labels=self._labels(labels),
            hidden_by=er.RegistryEntryHider.USER if hidden else None,
        )
        self.hass.states.async_set(entity_id, "on")

    async def registry(self) -> Any:
        return await self.client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )

    async def devices(self) -> Any:
        return await self.client.send_websocket_message(
            {"type": "config/device_registry/list"}
        )

    async def states(self) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = await self.client.get_states()
        return states

    async def hidden(self, config: VisibilityConfig, **kwargs: Any) -> set[str]:
        hidden, _ = hidden_entity_ids(
            await self.registry(),
            config,
            await self.states(),
            device_registry_result=await self.devices(),
            **kwargs,
        )
        return hidden


@pytest.fixture
async def core(hass: HomeAssistant, core_client: HomeAssistantClient) -> Core:
    assert await async_setup_component(hass, "config", {})
    return Core(hass, core_client)


@pytest.fixture
async def unconfigured(core_client: HomeAssistantClient) -> Any:
    """Core refuses the registry list: its ``config`` integration is not set up."""
    reply = await core_client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    assert reply["success"] is False
    return reply


# --- an unusable registry ----------------------------------------------------------


async def test_an_unusable_registry_fails_open_and_says_so(
    unconfigured: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        hidden, warnings = hidden_entity_ids(
            unconfigured, VisibilityConfig(enabled=True)
        )

    assert hidden == set()
    assert any("unavailable" in w for w in warnings)
    assert any("unusable" in r.message for r in caplog.records)


async def test_a_registry_read_that_got_no_answer_fails_open(
    core: Core, caplog: pytest.LogCaptureFixture
) -> None:
    """The callers gather the reads with ``return_exceptions``: the payload is
    the transport error itself."""
    with patch.object(
        core.client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
    ):
        payloads = await asyncio.gather(core.registry(), return_exceptions=True)
    payload = payloads[0]

    with caplog.at_level(logging.WARNING):
        hidden, _ = hidden_entity_ids(payload, VisibilityConfig(enabled=True))

    assert hidden == set()
    assert any("unusable" in r.message for r in caplog.records)


async def test_a_disabled_filter_is_silent_on_an_unusable_registry(
    unconfigured: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Disabled is the default: it must not warn on every call."""
    with caplog.at_level(logging.WARNING):
        result = hidden_entity_ids(unconfigured, VisibilityConfig(enabled=False))

    assert result == (set(), [])
    assert not caplog.records


async def test_an_unusable_registry_still_hides_the_denied_entity(
    unconfigured: Any,
) -> None:
    hidden, warnings = hidden_entity_ids(
        unconfigured, _cfg(deny_entity_ids=["sensor.ghost"])
    )

    assert hidden == {"sensor.ghost"}
    assert any("unavailable" in w for w in warnings)


async def test_an_unusable_registry_keeps_the_entity_allowlist(
    hass: HomeAssistant, core_client: HomeAssistantClient, unconfigured: Any
) -> None:
    """allow_entity_ids needs no registry: entities outside it stay hidden, the
    listed one visible, and deny still wins."""
    for entity_id in ("light.allowed", "light.allowed_but_denied", "light.other"):
        hass.states.async_set(entity_id, "on")
    config = _cfg(
        deny_entity_ids=["light.allowed_but_denied"],
        allow_entity_ids=["light.allowed", "light.allowed_but_denied"],
        allow_areas=["kitchen"],
    )

    hidden, warnings = hidden_entity_ids(
        unconfigured, config, await core_client.get_states()
    )

    assert hidden == {"light.allowed_but_denied", "light.other"}
    assert any("unavailable" in w for w in warnings)


async def test_without_an_entity_allowlist_an_unusable_registry_hides_only_denied(
    hass: HomeAssistant, core_client: HomeAssistantClient, unconfigured: Any
) -> None:
    hass.states.async_set("light.denied", "on")
    hass.states.async_set("light.other", "on")
    config = _cfg(deny_entity_ids=["light.denied"], allow_areas=["kitchen"])

    hidden, _ = hidden_entity_ids(unconfigured, config, await core_client.get_states())

    assert hidden == {"light.denied"}


async def test_strict_mode_fails_closed_on_an_unusable_registry(
    unconfigured: Any,
) -> None:
    """The enforcement path never leaks on degraded data."""
    with pytest.raises(VisibilityDataUnavailable):
        hidden_entity_ids(unconfigured, _cfg(exclude_areas=["office"]), strict=True)


# --- which configs can hide anything (the ha_search routing gate) -------------------


def test_a_disabled_config_has_no_active_dimension() -> None:
    config = VisibilityConfig(enabled=False, deny_entity_ids=["light.x"])
    assert config_has_active_hide_dimensions(config) is False


def test_an_enabled_config_with_nothing_set_has_no_active_dimension() -> None:
    assert config_has_active_hide_dimensions(_cfg()) is False


def test_the_default_enabled_config_hides_its_default_categories() -> None:
    assert config_has_active_hide_dimensions(VisibilityConfig(enabled=True)) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"deny_entity_ids": ["light.x"]},
        {"exclude_hidden": True},
        {"exclude_areas": ["garage"]},
        {"exclude_labels": ["hidden"]},
        {"allow_entity_ids": ["light.only"]},
        {"allow_areas": ["kitchen"]},
        {"allow_labels": ["ok"]},
        {"respect_assist_exposure": True},
    ],
)
def test_each_dimension_activates_the_filter(kwargs: dict[str, Any]) -> None:
    assert config_has_active_hide_dimensions(_cfg(**kwargs)) is True


def test_an_unknown_category_alone_activates_nothing() -> None:
    """A typo'd category is none the resolver would hide on."""
    config = VisibilityConfig(enabled=True, exclude_categories=["typo_not_a_category"])
    assert config_has_active_hide_dimensions(config) is False


def _active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    return asyncio.run(visibility_filter_active())


@pytest.mark.parametrize(
    ("config", "active"),
    [
        (None, False),
        (_cfg(deny_entity_ids=["input_boolean.probe"]), True),
        (VisibilityConfig(enabled=False), False),
    ],
    ids=["no-file", "enabled-deny", "disabled"],
)
def test_the_saved_config_decides_whether_the_filter_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: VisibilityConfig | None,
    active: bool,
) -> None:
    if config is not None:
        save_visibility_config(tmp_path, config)
    assert _active(tmp_path, monkeypatch) is active


def test_a_malformed_config_keeps_the_filtered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config that cannot be read must not route to the unfiltered component."""
    (tmp_path / "entity_visibility.json").write_text("{not json", encoding="utf-8")
    assert _active(tmp_path, monkeypatch) is True


# --- categories, hidden, deny -------------------------------------------------------


async def test_categories_hide_only_when_enabled(core: Core) -> None:
    """The default excludes both diagnostic and config entities."""
    core.entity("sensor.batt", category=EntityCategory.DIAGNOSTIC)
    core.entity("number.cfg", category=EntityCategory.CONFIG)
    core.entity("light.lr")

    assert await core.hidden(VisibilityConfig(enabled=False)) == set()
    assert await core.hidden(_cfg(exclude_categories=["diagnostic"])) == {"sensor.batt"}
    hidden, warnings = hidden_entity_ids(
        await core.registry(), VisibilityConfig(enabled=True)
    )
    assert (hidden, warnings) == ({"sensor.batt", "number.cfg"}, [])


async def test_an_unknown_category_is_dropped_with_a_warning(core: Core) -> None:
    core.entity("sensor.d", category=EntityCategory.DIAGNOSTIC)

    hidden, warnings = hidden_entity_ids(
        await core.registry(),
        VisibilityConfig(enabled=True, exclude_categories=["diagnostic", "typo"]),
    )

    assert hidden == {"sensor.d"}
    assert any("typo" in w for w in warnings)


async def test_exclude_hidden_hides_what_the_user_hid(core: Core) -> None:
    core.entity("sensor.h", hidden=True)
    core.entity("sensor.v")

    assert await core.hidden(_cfg(exclude_hidden=True)) == {"sensor.h"}


async def test_deny_hides_an_entity_with_no_registry_row(core: Core) -> None:
    """Deny matches the entity_id; only the registry dimensions need a row."""
    config = _cfg(deny_entity_ids=["sensor.ghost"])
    assert await core.hidden(config) == {"sensor.ghost"}
    core.entity("sensor.registered")
    assert await core.hidden(config) == {"sensor.ghost"}


async def test_deny_area_and_label_add_up(core: Core) -> None:
    core.entity("sensor.x", area="garage")
    core.entity("sensor.y", labels=("noise",))
    core.entity("sensor.z")
    config = _cfg(
        deny_entity_ids=["sensor.z"], exclude_areas=["garage"], exclude_labels=["noise"]
    )

    assert await core.hidden(config) == {"sensor.x", "sensor.y", "sensor.z"}


# --- what an entity inherits from its device ----------------------------------------


async def test_a_device_bound_entity_inherits_its_devices_area(core: Core) -> None:
    """Most entities have no area of their own; excluding the device's area
    hides them, allowing it keeps them."""
    spot = core.device("d1", area="garage")
    other = core.device("d2", area="attic")
    core.entity("light.spot", device_id=spot)
    core.entity("light.other", device_id=other)

    assert await core.hidden(_cfg(exclude_areas=["garage"])) == {"light.spot"}
    assert await core.hidden(_cfg(allow_areas=["garage"])) == {"light.other"}


async def test_a_child_devices_entity_inherits_its_parents_area(core: Core) -> None:
    parent = core.device("parent", area="office")
    child = core.device("child", parent=parent)
    core.entity("sensor.child", device_id=child)

    assert await core.hidden(_cfg(exclude_areas=["office"])) == {"sensor.child"}


async def test_an_entitys_own_area_wins_over_its_devices(core: Core) -> None:
    spot = core.device("d1", area="garage")
    core.entity("light.spot", area="kitchen", device_id=spot)

    assert await core.hidden(_cfg(exclude_areas=["garage"])) == set()
    assert await core.hidden(_cfg(exclude_areas=["kitchen"])) == {"light.spot"}


async def test_an_entitys_labels_are_its_own_and_its_devices(core: Core) -> None:
    spot = core.device("d1", labels=("dev",))
    other = core.device("d2", labels=("misc",))
    core.entity("light.spot", labels=("own",), device_id=spot)
    core.entity("light.other", device_id=other)

    assert await core.hidden(_cfg(exclude_labels=["own"])) == {"light.spot"}
    assert await core.hidden(_cfg(exclude_labels=["dev"])) == {"light.spot"}
    assert await core.hidden(_cfg(allow_labels=["dev"])) == {"light.other"}


async def test_without_the_device_list_only_entity_areas_match(core: Core) -> None:
    """The degraded read: a device-bound entity is not matched, one with its
    own area still is."""
    bound = core.device("d1", area="garage")
    core.entity("light.device_bound", device_id=bound)
    core.entity("light.entity_area", area="garage")

    hidden, _ = hidden_entity_ids(await core.registry(), _cfg(exclude_areas=["garage"]))

    assert hidden == {"light.entity_area"}


# --- the allowlist (restrict mode) -----------------------------------------------------


async def test_an_allowlist_hides_everything_it_does_not_name(core: Core) -> None:
    core.entity("light.kitchen", area="kitchen", labels=("voice",))
    core.entity("light.bedroom", area="bedroom", labels=("other",))
    hass = core.hass

    assert await core.hidden(_cfg(allow_areas=["kitchen"])) == {"light.bedroom"}
    assert await core.hidden(_cfg(allow_labels=["voice"])) == {"light.bedroom"}
    assert await core.hidden(_cfg(allow_entity_ids=["light.kitchen"])) == {
        "light.bedroom"
    }
    # An entity Core holds only as a state is hidden unless named.
    hass.states.async_set("sensor.yaml_only", "1")
    assert "sensor.yaml_only" in await core.hidden(_cfg(allow_areas=["kitchen"]))
    assert (
        await core.hidden(
            _cfg(
                allow_entity_ids=["light.kitchen", "light.bedroom", "sensor.yaml_only"]
            )
        )
        == set()
    )


async def test_no_allowlist_hides_nothing(core: Core) -> None:
    core.entity("light.a", area="kitchen")

    assert await core.hidden(_cfg()) == set()


async def test_deny_and_an_explicit_area_exclude_win_over_the_allowlist(
    core: Core,
) -> None:
    core.entity("light.kitchen", area="kitchen")
    core.entity("light.conflict", area="office")

    assert await core.hidden(
        _cfg(allow_areas=["kitchen"], deny_entity_ids=["light.kitchen"])
    ) == {"light.kitchen", "light.conflict"}
    assert await core.hidden(
        _cfg(
            exclude_areas=["office"],
            allow_entity_ids=["light.conflict", "light.kitchen"],
        )
    ) == {"light.conflict"}


async def test_an_allowlist_match_overrides_the_category_and_hidden_filters(
    core: Core,
) -> None:
    """The allowlist authorizes what it names; broad automatic filters do not
    hide it again."""
    core.entity(
        "sensor.diag_allowed",
        category=EntityCategory.DIAGNOSTIC,
        hidden=True,
        area="kitchen",
    )
    core.entity("light.kitchen", area="kitchen")
    core.entity("light.bedroom", area="bedroom")
    config = VisibilityConfig(
        enabled=True,
        exclude_categories=["diagnostic"],
        exclude_hidden=True,
        allow_areas=["kitchen"],
    )

    assert await core.hidden(config) == {"light.bedroom"}


async def test_an_empty_registry_drops_an_area_allowlist_instead_of_blanking(
    core: Core,
) -> None:
    """The registry answered but holds nothing: the area allowlist would hide
    every entity, so it is skipped with a warning; allow_entity_ids still holds."""
    core.hass.states.async_set("light.a", "on")
    core.hass.states.async_set("sensor.b", "1")
    registry, states = await core.registry(), await core.states()

    open_hidden, warnings = hidden_entity_ids(
        registry, _cfg(allow_areas=["kitchen"]), states
    )
    named_hidden, _ = hidden_entity_ids(
        registry, _cfg(allow_areas=["kitchen"], allow_entity_ids=["light.a"]), states
    )

    assert open_hidden == set()
    assert any("registry returned no entries" in w for w in warnings)
    assert named_hidden == {"sensor.b"}


# --- Assist exposure -------------------------------------------------------------------


@pytest.fixture
async def assist(core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Core's Assist exposure, and the resolver over a saved config."""
    assert await async_setup_component(core.hass, "homeassistant", {})
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    async def resolve(config: VisibilityConfig, *, expose_new: bool) -> set[str]:
        reply = await core.client.send_websocket_message(
            {
                "type": "homeassistant/expose_new_entities/set",
                "assistant": "conversation",
                "expose_new": expose_new,
            }
        )
        assert reply["success"], reply
        save_visibility_config(tmp_path, config)
        hidden, _ = await load_hidden_set(
            await core.registry(),
            await core.states(),
            core.client,
            await core.devices(),
        )
        return hidden

    return resolve


def _assist_cfg(**kw: Any) -> VisibilityConfig:
    return _cfg(respect_assist_exposure=True, **kw)


async def test_an_exposed_entity_shows_even_in_a_hidden_category(
    core: Core, assist: Any
) -> None:
    """An explicit setting wins outright, as in Core's async_should_expose."""
    core.entity("sensor.x", category=EntityCategory.DIAGNOSTIC)
    async_expose_entity(core.hass, "conversation", "sensor.x", True)

    assert await assist(_assist_cfg(), expose_new=True) == set()


@pytest.mark.parametrize(
    ("entity_id", "device_class", "hidden"),
    [
        ("light.a", None, False),
        ("sensor.random", None, True),
        ("binary_sensor.front", "door", False),
    ],
    ids=["default-domain", "other-domain", "default-device-class"],
)
async def test_new_entities_follow_cores_default_exposure(
    core: Core, assist: Any, entity_id: str, device_class: str | None, hidden: bool
) -> None:
    core.entity(entity_id, device_class=device_class)
    # The entity writes its device class into its state, as Core's do.
    attributes = {"device_class": device_class} if device_class else {}
    core.hass.states.async_set(entity_id, "on", attributes)

    assert (entity_id in await assist(_assist_cfg(), expose_new=True)) is hidden


async def test_a_device_class_core_reads_from_the_state_counts(
    core: Core, assist: Any
) -> None:
    core.entity("sensor.temp")
    core.hass.states.async_set("sensor.temp", "21", {"device_class": "temperature"})

    assert await assist(_assist_cfg(), expose_new=True) == set()


async def test_a_category_entity_is_never_exposed_by_default(
    core: Core, assist: Any
) -> None:
    core.entity("light.cfg", category=EntityCategory.CONFIG)

    assert await assist(_assist_cfg(), expose_new=True) == {"light.cfg"}


async def test_without_expose_new_an_unconfigured_entity_is_hidden(
    core: Core, assist: Any
) -> None:
    core.entity("light.a")

    assert await assist(_assist_cfg(), expose_new=False) == {"light.a"}


async def test_an_allowlist_match_overrides_assist_exposure(
    core: Core, assist: Any
) -> None:
    core.entity("sensor.allowed", category=EntityCategory.DIAGNOSTIC)
    core.entity("sensor.not_allowed")
    for entity_id in ("sensor.allowed", "sensor.not_allowed"):
        async_expose_entity(core.hass, "conversation", entity_id, False)
    config = VisibilityConfig(
        enabled=True,
        exclude_categories=["diagnostic"],
        allow_entity_ids=["sensor.allowed"],
        respect_assist_exposure=True,
    )

    assert await assist(config, expose_new=False) == {"sensor.not_allowed"}
