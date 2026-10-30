"""ha_mcp_tools entity_entry / entity_restore: an entity backup written in process.

The fake registry takes what Core's ``EntityRegistry`` takes: an entity_id or
an entry id in ``async_get``, hidden_by and disabled_by as their enums, the whole
``categories`` dict, and a ValueError before anything is written for a taken
entity_id.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# The sibling module installs the homeassistant.* stubs the component needs, so
# the component modules are imported only after it.
from ha_mcp import backup_entities

from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    _FakeConnection,
    _Unauthorized,
    functional_ws,  # noqa: F401 - pytest fixture
    wsapi,
)

er_mod = importlib.import_module("custom_components.ha_mcp_tools.entity_restore")


class Hider(enum.StrEnum):
    INTEGRATION = "integration"
    USER = "user"


class Disabler(enum.StrEnum):
    CONFIG_ENTRY = "config_entry"
    DEVICE = "device"
    HASS = "hass"
    INTEGRATION = "integration"
    USER = "user"


COMPUTED_NAME = object()
ER = SimpleNamespace(
    COMPUTED_NAME=COMPUTED_NAME,
    RegistryEntryHider=Hider,
    RegistryEntryDisabler=Disabler,
    ORPHANED_ENTITY_KEEP_SECONDS=3600 * 24 * 30,
)


@dataclasses.dataclass(frozen=True)
class Entry:
    id: str
    entity_id: str
    name: str | None = None
    icon: str | None = None
    area_id: str | None = None
    device_class: str | None = None
    aliases: tuple[Any, ...] = ()
    labels: frozenset[str] = frozenset()
    categories: dict[str, str] = dataclasses.field(default_factory=dict)
    hidden_by: Hider | None = None
    disabled_by: Disabler | None = None
    options: dict[str, Any] = dataclasses.field(default_factory=dict)
    device_id: str | None = "dev1"
    config_entry_id: str | None = "ce1"

    @property
    def extended_dict(self) -> dict[str, Any]:
        return {"id": self.id, "entity_id": self.entity_id, "name": self.name}


class FakeRegistry:
    def __init__(self, *entries: Entry) -> None:
        self.entities = {e.entity_id: e for e in entries}
        self.deleted_entities: dict[tuple[str, str, str], Any] = {}
        self.calls: list[tuple[str, Any]] = []

    def async_get(self, entity_id_or_uuid: str) -> Entry | None:
        return self.entities.get(entity_id_or_uuid) or next(
            (e for e in self.entities.values() if e.id == entity_id_or_uuid), None
        )

    def async_update_entity(self, entity_id: str, **changes: Any) -> Entry:
        self.calls.append(("update", changes))
        old = self.entities[entity_id]
        new_id = changes.pop("new_entity_id", entity_id)
        if new_id != entity_id and new_id in self.entities:
            raise ValueError("Entity with this ID is already registered")
        for key in ("hidden_by", "disabled_by"):
            assert changes.get(key) is None or isinstance(changes[key], enum.Enum)
        if "aliases" in changes:
            changes["aliases"] = tuple(changes["aliases"])
        if "labels" in changes:
            assert isinstance(changes["labels"], set)
            changes["labels"] = frozenset(changes["labels"])
        new = dataclasses.replace(old, entity_id=new_id, **changes)
        del self.entities[entity_id]
        self.entities[new_id] = new
        return new

    def async_update_entity_options(
        self, entity_id: str, domain: str, options: dict[str, Any] | None
    ) -> Entry:
        self.calls.append(("options", domain))
        old = self.entities[entity_id]
        new_options = {d: o for d, o in old.options.items() if d != domain}
        if options is not None:
            new_options[domain] = options
        self.entities[entity_id] = dataclasses.replace(old, options=new_options)
        return self.entities[entity_id]


DESK = Entry(
    id="reg-desk",
    entity_id="light.desk",
    name="Desk",
    area_id="kitchen",
    labels=frozenset({"x"}),
    categories={"automation": "cat-b", "script": "cat-s"},
    hidden_by=Hider.USER,
    options={"conversation": {"should_expose": True}},
)
SETTINGS = {
    "name": None,
    "icon": "mdi:lamp",
    "area_id": "office",
    "device_class": "outlet",
    "aliases": [None, "lamp"],
    "labels": ["work"],
    "categories": {"automation": "cat-a"},
    "hidden_by": "integration",
}


@pytest.fixture
def parents(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The device and config entry, and the exposure calls Core gets."""
    state = SimpleNamespace(device_disabled=False, entry_disabled=False, exposed=[])
    device_registry = SimpleNamespace(
        async_get=lambda hass: SimpleNamespace(
            async_get=lambda device_id: SimpleNamespace(
                disabled=state.device_disabled and device_id == DESK.device_id
            )
        )
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "device_registry", device_registry
    )
    exposed = SimpleNamespace(
        async_expose_entity=lambda hass, assistant, entity_id, value: (
            state.exposed.append(("expose", assistant, entity_id, value))
        )
    )
    package = "homeassistant.components.homeassistant"
    monkeypatch.setitem(sys.modules, package, SimpleNamespace(exposed_entities=exposed))
    monkeypatch.setitem(sys.modules, f"{package}.exposed_entities", exposed)
    state.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: SimpleNamespace(
                disabled_by="user" if state.entry_disabled else None
            )
        )
    )
    return state


def _restore(registry: FakeRegistry, parents: Any, **msg: Any) -> dict[str, Any]:
    msg = {
        "entry_id": "reg-desk",
        "entity_id": "light.desk",
        "settings": SETTINGS,
        **msg,
    }
    return er_mod.write_entry(parents.hass, registry, ER, msg)


def test_every_field_goes_in_one_update(parents: Any) -> None:
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, new_entity_id="light.lamp")

    entry = registry.entities["light.lamp"]
    assert result == {
        "success": True,
        "entity_entry": entry.extended_dict,
        "warnings": [],
    }
    assert [kind for kind, _ in registry.calls] == ["update"]
    assert entry.categories == {"automation": "cat-a"}
    assert entry.hidden_by is Hider.INTEGRATION
    assert entry.aliases == (COMPUTED_NAME, "lamp")
    assert (entry.name, entry.icon, entry.area_id, entry.device_class) == (
        None,
        "mdi:lamp",
        "office",
        "outlet",
    )
    assert entry.labels == frozenset({"work"})


def test_a_hidden_entity_is_shown_again(parents: Any) -> None:
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, settings={**SETTINGS, "hidden_by": None})

    assert result["success"] is True
    assert registry.entities["light.desk"].hidden_by is None


def test_the_entry_is_found_by_its_id_only(parents: Any) -> None:
    """An entity_id in entry_id must not pick that entity."""
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, entry_id="light.desk")

    assert result["error"]["code"] == "entry_deleted"
    assert registry.calls == []


def test_a_renamed_entry_is_not_written(parents: Any) -> None:
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, entity_id="light.other")

    assert result["error"]["code"] == "entity_identity_mismatch"
    assert registry.calls == []


def test_cores_refusal_writes_nothing(parents: Any) -> None:
    registry = FakeRegistry(DESK, Entry(id="reg-lamp", entity_id="light.lamp"))

    result = _restore(registry, parents, new_entity_id="light.lamp")

    assert result == {
        "success": False,
        "error": {
            "code": "invalid",
            "message": "Entity with this ID is already registered",
        },
    }
    assert registry.entities["light.desk"] == DESK


def test_an_unknown_hider_value_is_invalid(parents: Any) -> None:
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, settings={**SETTINGS, "hidden_by": "nobody"})

    assert result["error"]["code"] == "invalid"
    assert registry.entities["light.desk"] == DESK


@pytest.mark.parametrize(
    ("saved", "device_disabled", "entry_disabled", "written"),
    [
        ("user", False, False, Disabler.USER),
        ("hass", False, False, Disabler.HASS),
        ("device", True, False, Disabler.DEVICE),
        ("config_entry", False, True, Disabler.CONFIG_ENTRY),
        ("device", False, False, None),
        ("config_entry", False, False, None),
    ],
)
def test_a_disabler_core_sets_from_a_parent_needs_that_parent_disabled(
    parents: Any,
    saved: str,
    device_disabled: bool,
    entry_disabled: bool,
    written: Disabler | None,
) -> None:
    """A device or config_entry value is written only while that parent is
    disabled: Core resets it on the parent's next update otherwise."""
    parents.device_disabled = device_disabled
    parents.entry_disabled = entry_disabled
    registry = FakeRegistry(DESK)

    result = _restore(registry, parents, settings={**SETTINGS, "disabled_by": saved})

    assert registry.entities["light.desk"].disabled_by is written
    if written is None:
        [warning] = result["warnings"]
        assert f"the backup holds {saved!r}" in warning
    else:
        assert result["warnings"] == []


def test_the_disabled_device_must_be_the_entitys_own(parents: Any) -> None:
    parents.device_disabled = True
    registry = FakeRegistry(dataclasses.replace(DESK, device_id="dev2"))

    result = _restore(registry, parents, settings={**SETTINGS, "disabled_by": "device"})

    assert registry.entities["light.desk"].disabled_by is None
    [warning] = result["warnings"]
    assert "the device is not disabled now" in warning


def test_exposure_goes_to_core_before_the_options_write(parents: Any) -> None:
    """Core tells the assistant only when its expose call changes the value."""
    registry = FakeRegistry(DESK)
    registry.calls = parents.exposed

    _restore(
        registry,
        parents,
        options={
            "conversation": {"should_expose": False},
            "light": None,
            "sensor": {"display_precision": 2},
        },
    )

    assert registry.entities["light.desk"].options == {
        "conversation": {"should_expose": False},
        "sensor": {"display_precision": 2},
    }
    assert parents.exposed[1:] == [
        ("expose", "conversation", "light.desk", False),
        ("options", "conversation"),
        ("options", "light"),
        ("options", "sensor"),
    ]


def test_unchanged_exposure_is_not_sent(parents: Any) -> None:
    registry = FakeRegistry(DESK)

    _restore(registry, parents, options={"conversation": {"should_expose": True}})

    assert parents.exposed == []


def _deleted(**fields: Any) -> SimpleNamespace:
    base = {
        "id": "reg-desk",
        "entity_id": "light.desk",
        "config_entry_id": None,
        "orphaned_timestamp": None,
    }
    return SimpleNamespace(**{**base, **fields})


@pytest.mark.parametrize(
    ("deleted", "answer"),
    [
        (
            _deleted(config_entry_id="ce1"),
            {"entity_id": "light.desk", "config_entry_id": "ce1", "kept_until": None},
        ),
        (
            _deleted(orphaned_timestamp=1_800_000_000.0),
            {
                "entity_id": "light.desk",
                "config_entry_id": None,
                "kept_until": "2027-02-14T08:00:00+00:00",
            },
        ),
        (_deleted(id="reg-other"), None),
    ],
)
def test_a_removed_entry_says_what_core_keeps(
    parents: Any, deleted: Any, answer: dict[str, Any] | None
) -> None:
    registry = FakeRegistry()
    registry.deleted_entities[("light", "hue", "u")] = deleted

    read = er_mod.read_entry(registry, ER, "reg-desk")
    write = _restore(registry, parents)

    assert read == {"entry": None, "deleted": answer}
    assert write["error"]["code"] == "entry_deleted"
    assert write["error"]["deleted"] == answer


def test_a_live_entry_reads_its_current_entity_id() -> None:
    registry = FakeRegistry(DESK)

    assert er_mod.read_entry(registry, ER, "reg-desk") == {
        "entry": {"id": "reg-desk", "entity_id": "light.desk"}
    }
    assert er_mod.read_entry(registry, ER, "light.desk")["entry"] is None


def test_commands_registered_and_advertised(functional_ws) -> None:  # noqa: F811
    assert set(er_mod.COMMANDS) <= set(functional_ws.registered)
    assert {"entity_entry", "entity_restore"} <= set(wsapi.CAPABILITIES)


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (er_mod.WS_ENTITY_ENTRY, {"entry_id": "reg-desk"}),
        (
            er_mod.WS_ENTITY_RESTORE,
            {"entry_id": "reg-desk", "entity_id": "light.desk", "settings": {}},
        ),
    ],
)
def test_commands_are_admin_gated(functional_ws, command, extra) -> None:  # noqa: F811
    handler = functional_ws.registered[command]
    msg = {"id": 1, "type": command, **extra}
    with pytest.raises(_Unauthorized):
        handler(FakeHass(), _FakeConnection(is_admin=False), msg)


def test_each_command_runs_its_own_handler(parents: Any) -> None:
    registry = FakeRegistry(DESK)
    wired = SimpleNamespace(**vars(ER), async_get=lambda hass: registry)
    handlers = {
        schema["type"]: do_fn
        for schema, do_fn, _prep in er_mod.command_specs(_REAL_VOL, wired)
    }
    read = handlers[er_mod.WS_ENTITY_ENTRY]
    write = handlers[er_mod.WS_ENTITY_RESTORE]
    msg = {"entry_id": "reg-desk", "entity_id": "light.desk", "settings": SETTINGS}

    found = read(parents.hass, msg)
    written = write(parents.hass, msg)

    assert found["entry"]["entity_id"] == "light.desk"
    assert written["success"] is True
    assert registry.entities["light.desk"].icon == "mdi:lamp"


SNAPSHOT = {
    "id": "reg-desk",
    "entity_id": "light.desk",
    "name": None,
    "icon": None,
    "area_id": "office",
    "device_class": None,
    "aliases": [None, "desk lamp"],
    "labels": ["work"],
    "categories": {"automation": "cat-a"},
    "hidden_by": "integration",
    "disabled_by": "user",
    "options": {"conversation": {"should_expose": False}},
}


def test_the_servers_message_passes_the_command_schema(monkeypatch) -> None:
    """Whatever the server sends, the component's schema takes it, a removed
    options domain and a renamed entity included."""
    sent: list[dict[str, Any]] = []

    async def capture(client: Any, capability: str, message: dict[str, Any]) -> Any:
        sent.append(message)
        return {"success": True, "entity_entry": {}, "warnings": []}

    monkeypatch.setattr(backup_entities, "_component_send", capture)
    current = {**SNAPSHOT, "options": {"light": {"x": 1}}, "disabled_by": None}
    asyncio.run(
        backup_entities._restore_in_process(
            None, "light.desk", "reg-desk", "light.lamp", current, SNAPSHOT
        )
    )
    schema = _REAL_VOL.Schema(er_mod.command_specs(_REAL_VOL, ER)[1][0])

    [message] = sent
    assert schema(message) == message
    assert message["options"] == {
        "conversation": {"should_expose": False},
        "light": None,
    }
    for broken in (
        {**message, "settings": {**message["settings"], "labels": [1]}},
        {key: value for key, value in message.items() if key != "settings"},
    ):
        with pytest.raises(_REAL_VOL.Invalid):
            schema(broken)
