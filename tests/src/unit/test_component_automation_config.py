"""ha_mcp_tools/automation_config: Core's automation validator, descriptions and lookup.

The component runs against a fake Core: a validator that reports a status the
way ``_async_validate_config_item`` does and rewrites the config it is given,
description getters, an entity registry, an issue registry, the loaded
automation entities and the ``*_in_automation`` reference helpers.
"""

from __future__ import annotations

import copy
import importlib
import sys
import types
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

import pytest

from .test_component_ws_search import (
    _REAL_VOL,
    FakeEntityReg,
    FakeHass,
    FakeRegEntry,
    _exceptions_stub,
    _FakeConnection,
    _StubHomeAssistantError,
    _Unauthorized,
    functional_ws,  # noqa: F401 - pytest fixture
    wsapi,
)

automation_config = importlib.import_module(
    "custom_components.ha_mcp_tools.automation_config"
)

MORNING = "automation.morning"


class _Status(StrEnum):
    # Core's automation ValidationStatus members.
    FAILED_ACTIONS = "failed_actions"
    FAILED_BLUEPRINT = "failed_blueprint"
    FAILED_CONDITIONS = "failed_conditions"
    FAILED_SCHEMA = "failed_schema"
    FAILED_TRIGGERS = "failed_triggers"
    OK = "ok"


class _Invalid(Exception):
    pass


class _Checked:
    """Core's AutomationConfig as the bridge reads it: the outcome attributes."""

    def __init__(self, status: _Status, error: str | None = None) -> None:
        self.validation_status = status
        self.validation_error = error


class _Validator:
    def __init__(self) -> None:
        self.answer: Any = _Checked(_Status.OK)
        self.raise_: Exception | None = None
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(
        self, hass: Any, config: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        self.calls.append((*args, *kwargs.items()))
        # Core's backward-compat schema renames keys in the dict it is given.
        if "trigger" in config:
            config["triggers"] = config.pop("trigger")
        if self.raise_ is not None:
            raise self.raise_
        return self.answer


class _Issue:
    def __init__(self, error: str, *, active: bool = True) -> None:
        self.active = active
        self.translation_placeholders = (
            {"entity_id": MORNING, "error": error} if active else None
        )


class _Issues:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, str], _Issue] = {}

    def async_get_issue(self, domain: str, issue_id: str) -> _Issue | None:
        return self.issues.get((domain, issue_id))


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    registry = FakeEntityReg(
        {
            MORNING: FakeRegEntry(
                MORNING, unique_id="1700", platform="automation", labels=("l1",)
            ),
            "light.porch": FakeRegEntry(
                "light.porch", unique_id="1700", platform="hue"
            ),
        }
    )
    issues = _Issues()
    loaded: set[str] = set()
    references = {
        "entities": {"light.porch", "light.hall"},
        "devices": {"d1"},
        "areas": set(),
        "floors": {"f1"},
        "labels": {"l2", "l1"},
    }
    ns = SimpleNamespace(
        validate=_Validator(),
        validate_or_raise=None,
        invalid=(_Invalid, _StubHomeAssistantError),
        statuses=_Status,
        descriptions={"trigger": None, "condition": None},
        entity_registry=lambda hass: registry,
        issue_registry=lambda hass: issues,
        component_key="automation_component",
        references={
            name: (lambda hass, entity_id, ids=ids: set(ids))
            for name, ids in references.items()
        },
        blueprint=lambda hass, entity_id: "motion_light.yaml",
        # test handles
        issues=issues,
        loaded=loaded,
    )
    monkeypatch.setattr(automation_config, "_core", lambda: ns)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", _exceptions_stub)
    return ns


def _hass(core: SimpleNamespace) -> Any:
    component = SimpleNamespace(
        get_entity=lambda entity_id: object() if entity_id in core.loaded else None
    )
    return SimpleNamespace(data={core.component_key: component})


async def _run(core: SimpleNamespace, action: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = await automation_config.async_automation_config(
        _hass(core), {"action": action, **fields}
    )
    return result


# --- validate -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "wire"),
    [
        (_Status.OK, None, "ok"),
        (
            _Status.FAILED_TRIGGERS,
            "Invalid trigger 'stat' specified",
            "failed_triggers",
        ),
    ],
)
async def test_validation_reports_cores_status_and_text(
    core: SimpleNamespace, status: _Status, error: str | None, wire: str
) -> None:
    core.validate.answer = _Checked(status, error)
    got = await _run(core, "validate", config={"triggers": []})
    assert got == {"status": wire, "error": error}


async def test_validation_leaves_the_callers_config_unchanged(
    core: SimpleNamespace,
) -> None:
    config = {"trigger": [{"trigger": "state"}], "actions": []}
    sent = copy.deepcopy(config)
    await _run(core, "validate", config=config)
    assert config == sent


async def test_a_proposal_is_not_raised_or_logged_as_a_broken_automation(
    core: SimpleNamespace,
) -> None:
    await _run(core, "validate", config={})
    assert core.validate.calls == [
        (("raise_on_errors", False), ("warn_on_errors", False))
    ]


async def test_config_core_cannot_even_name_is_a_schema_failure(
    core: SimpleNamespace,
) -> None:
    # Core's fallback id/alias/description check raises even when told not to.
    core.validate.raise_ = _Invalid("expected str for dictionary value @ data['alias']")
    got = await _run(core, "validate", config={"alias": ["x"]})
    assert got == {
        "status": "failed_schema",
        "error": "expected str for dictionary value @ data['alias']",
    }


async def test_public_validator_stands_in_for_a_missing_private_one(
    core: SimpleNamespace,
) -> None:
    async def public(hass: Any, config_key: str, config: dict[str, Any]) -> Any:
        # Core's public validator raises on every failure.
        raise _Invalid("required key not provided @ data['triggers']")

    core.validate, core.validate_or_raise = None, public
    got = await _run(core, "validate", config={})
    assert got == {
        "status": "failed_schema",
        "error": "required key not provided @ data['triggers']",
    }


async def test_core_without_a_validator_is_unavailable(core: SimpleNamespace) -> None:
    core.validate = None
    got = await _run(core, "validate", config={})
    assert got["status"] == "unavailable"


async def test_unexpected_core_failure_is_unavailable_not_an_error(
    core: SimpleNamespace,
) -> None:
    core.validate.raise_ = RuntimeError("blueprint store not loaded")
    got = await _run(core, "validate", config={})
    assert got == {"status": "unavailable", "error": "blueprint store not loaded"}


async def test_core_without_the_automation_integration_is_unavailable(
    core: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(automation_config, "_core", lambda: None)
    got = await _run(core, "validate", config={})
    assert got["status"] == "unavailable"


# --- describe -------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["trigger", "condition"])
async def test_descriptions_are_cores_own(core: SimpleNamespace, kind: str) -> None:
    # A platform without a description stays listed: its key is still valid.
    descriptions = {
        "state": {"fields": {"to": {"selector": {"state": {}}}}},
        "light.turned_on": {"target": {"entity": [{"domain": ["light"]}]}},
        "mqtt": None,
    }

    async def describe(hass: Any) -> dict[str, Any]:
        return descriptions

    core.descriptions[kind] = describe
    got = await _run(core, "describe", kind=kind)
    assert got == {"descriptions": descriptions}


async def test_core_without_descriptions_is_unavailable(core: SimpleNamespace) -> None:
    got = await _run(core, "describe", kind="condition")
    assert got["status"] == "unavailable"


# --- lookup ---------------------------------------------------------------------


async def test_lookup_by_entity_id_returns_cores_registry_row(
    core: SimpleNamespace,
) -> None:
    """The list row lacked the aliases and capabilities the fallback's
    config/entity_registry/get row carries, so the field changed shape with
    the route."""
    got = await _run(core, "lookup", entity_id=MORNING)
    row = core.entity_registry(None).async_get(MORNING).extended_dict
    assert got["found"] is True
    assert got["entity"] == row


async def test_lookup_by_config_id_resolves_through_the_registry(
    core: SimpleNamespace,
) -> None:
    got = await _run(core, "lookup", unique_id="1700")
    assert got["entity"]["entity_id"] == MORNING


@pytest.mark.parametrize(
    "fields", [{"entity_id": "automation.gone"}, {"entity_id": "light.porch"}]
)
async def test_lookup_without_an_automation_row_is_not_found(
    core: SimpleNamespace, fields: dict[str, str]
) -> None:
    got = await _run(core, "lookup", **fields)
    assert got == {
        "found": False,
        "entity": None,
        "loaded": False,
        "validation": None,
        "references": None,
    }


async def test_lookup_reports_the_validation_issue_core_filed(
    core: SimpleNamespace,
) -> None:
    core.issues.issues[("automation", f"{MORNING}_validation_failed_triggers")] = (
        _Issue("Invalid trigger 'stat' specified")
    )
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["validation"] == {
        "status": "failed_triggers",
        "error": "Invalid trigger 'stat' specified",
    }


async def test_issue_of_an_automation_whose_id_extends_this_one_is_ignored(
    core: SimpleNamespace,
) -> None:
    other = f"{MORNING}_validation_x_validation_failed_schema"
    core.issues.issues[("automation", other)] = _Issue("bad")
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["validation"] is None


async def test_stored_issue_from_an_earlier_run_is_ignored(
    core: SimpleNamespace,
) -> None:
    issue_id = f"{MORNING}_validation_failed_actions"
    core.issues.issues[("automation", issue_id)] = _Issue("old", active=False)
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["validation"] is None


async def test_lookup_carries_the_references_core_reads(core: SimpleNamespace) -> None:
    core.loaded.add(MORNING)
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["loaded"] is True
    assert got["references"] == {
        "entities": ["light.hall", "light.porch"],
        "devices": ["d1"],
        "areas": [],
        "floors": ["f1"],
        "labels": ["l1", "l2"],
        "blueprint": "motion_light.yaml",
    }


async def test_references_are_absent_while_the_automation_is_not_loaded(
    core: SimpleNamespace,
) -> None:
    """The server could not tell a disabled automation from one that
    references nothing; ``loaded`` lets it say why."""
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["found"] is True
    assert got["loaded"] is False
    assert got["references"] is None


async def test_lookup_without_a_key_is_refused_not_answered_not_found(
    core: SimpleNamespace,
) -> None:
    with pytest.raises(_StubHomeAssistantError):
        await _run(core, "lookup")


async def test_core_missing_a_lookup_part_is_unavailable(
    core: SimpleNamespace,
) -> None:
    core.references["floors"] = None
    got = await _run(core, "lookup", entity_id=MORNING)
    assert got["status"] == "unavailable"


# --- registration ---------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        {"action": "validate", "config": {"triggers": []}},
        {"action": "describe", "kind": "condition"},
        {"action": "lookup", "entity_id": MORNING, "unique_id": "1700"},
    ],
)
def test_schema_takes_each_action_the_server_sends(frame: dict[str, Any]) -> None:
    schema = _REAL_VOL.Schema(automation_config.command_specs(_REAL_VOL)[0][0])
    msg = {"type": automation_config.WS_AUTOMATION_CONFIG, **frame}
    assert schema(msg) == msg


def test_command_registered_and_advertised(functional_ws) -> None:  # noqa: F811
    assert automation_config.WS_AUTOMATION_CONFIG in functional_ws.registered
    assert automation_config.CAPABILITY in wsapi.CAPABILITIES


def test_command_is_admin_gated(
    functional_ws,  # noqa: F811
    core: SimpleNamespace,
) -> None:
    handler = functional_ws.registered[automation_config.WS_AUTOMATION_CONFIG]
    msg = {"id": 1, "type": automation_config.WS_AUTOMATION_CONFIG}
    msg |= {"action": "validate", "config": {}}
    with pytest.raises(_Unauthorized):
        handler(FakeHass(), _FakeConnection(is_admin=False), msg)
    connection = _FakeConnection()
    handler(FakeHass(), connection, msg)
    assert connection.results[1] == {"status": "ok", "error": None}


def test_component_key_is_read_from_the_package_on_older_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core 2026.10 defines DATA_COMPONENT in the automation package and only
    2026.11 moves it to automation.const; reading const alone left the lookup
    "unavailable" on every 2026.10 install."""
    components = types.ModuleType("homeassistant.components")
    automation = types.ModuleType("homeassistant.components.automation")
    automation.DATA_COMPONENT = "automation_component_key"
    for name in ("entities", "devices", "areas", "floors", "labels"):
        setattr(automation, f"{name}_in_automation", lambda hass, eid: set())
    automation.blueprint_in_automation = lambda hass, eid: None
    config = types.ModuleType("homeassistant.components.automation.config")
    const = types.ModuleType("homeassistant.components.automation.const")
    components.automation = automation
    automation.config, automation.const = config, const
    helpers = types.ModuleType("homeassistant.helpers")
    for name in ("condition", "trigger", "entity_registry", "issue_registry"):
        mod = types.ModuleType(f"homeassistant.helpers.{name}")
        setattr(helpers, name, mod)
        monkeypatch.setitem(sys.modules, f"homeassistant.helpers.{name}", mod)
    for name, mod in {
        "homeassistant.components": components,
        "homeassistant.components.automation": automation,
        "homeassistant.components.automation.config": config,
        "homeassistant.components.automation.const": const,
        "homeassistant.helpers": helpers,
        "homeassistant.exceptions": _exceptions_stub,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    core = automation_config._core()

    assert core is not None
    assert core.component_key == "automation_component_key"
