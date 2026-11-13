"""ha_mcp_tools/automation_config: Core's automation validator, descriptions and lookup.

The component runs in a real ``hass``. Automations load from the test's own
``automations.yaml``, so the registry rows, the loaded entities and the repair
issue of an automation that fails validation are the ones Core makes. A Core
build that lacks a part is the real module with that attribute removed.
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import AsyncMock

import probatio
import pytest
from homeassistant.components import automation as automation_core
from homeassistant.components.automation import config as automation_core_config
from homeassistant.components.automation import const as automation_core_const
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition, trigger
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import automation_config

from ._component_core import ws_call, ws_result
from ._config_dir_yaml import setup_from_configuration, write_config_files

MORNING = "automation.morning"
_BLUEPRINT_PATH = "ha_mcp_test/motion_light.yaml"
_BLUEPRINT = """\
blueprint:
  name: Motion light
  domain: automation
  input:
    motion:
      selector: {entity: {}}
    target:
      selector: {target: {}}
triggers:
  - trigger: state
    entity_id: !input motion
actions:
  - action: light.turn_on
    target: !input target
"""
# A working blueprint automation whose target names one of each reference.
_MORNING: dict[str, Any] = {
    "id": "1700",
    "alias": "morning",
    "use_blueprint": {
        "path": _BLUEPRINT_PATH,
        "input": {
            "motion": "light.porch",
            "target": {
                "entity_id": ["light.porch", "light.hall"],
                "device_id": "d1",
                "floor_id": "f1",
                "label_id": ["l2", "l1"],
            },
        },
    },
}
_BAD_TRIGGER: dict[str, Any] = {
    "id": "1700",
    "alias": "morning",
    "triggers": [{"trigger": "stat"}],
    "actions": [],
}


async def _load(hass: HomeAssistant, *automations: dict[str, Any]) -> None:
    """Load ``automations`` the way Core loads ``automations.yaml`` at start."""
    await write_config_files(
        hass,
        {
            f"blueprints/automation/{_BLUEPRINT_PATH}": _BLUEPRINT,
            "automations.yaml": dump(list(automations)),
            "configuration.yaml": "automation: !include automations.yaml\n",
        },
    )
    await setup_from_configuration(hass, "automation")


async def _command(client: MockHAClientWebSocket, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(
        client, automation_config.WS_AUTOMATION_CONFIG, **fields
    )
    return result


def _bridge_failed(caplog: pytest.LogCaptureFixture) -> bool:
    """Whether the component's catch-all for an unexpected failure ran."""
    return any(r.name == automation_config.__name__ for r in caplog.records)


# --- validate -------------------------------------------------------------------


async def test_valid_config_reports_ok(component_ws: MockHAClientWebSocket) -> None:
    got = await _command(
        component_ws, action="validate", config={"triggers": [], "actions": []}
    )
    assert got == {"status": "ok", "error": None}


async def test_validation_reports_what_cores_repair_issue_says(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A proposal gets the failed section and text the user would see in the
    repair issue once the automation is saved and loaded."""
    await _load(hass, _BAD_TRIGGER)
    issue = issue_registry.async_get_issue(
        "automation", f"{MORNING}_validation_failed_triggers"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None

    got = await _command(component_ws, action="validate", config=_BAD_TRIGGER)

    assert got == {
        "status": "failed_triggers",
        "error": issue.translation_placeholders["error"],
    }


async def test_validation_leaves_the_callers_config_unchanged(
    hass: HomeAssistant,
) -> None:
    """Core's backward-compat schema renames ``trigger`` to ``triggers`` in
    the dict it is given; the caller's own config must not change."""
    config = {
        "trigger": [{"trigger": "state", "entity_id": "light.porch"}],
        "actions": [],
    }
    sent = copy.deepcopy(config)
    await automation_config.async_automation_config(
        hass, {"action": "validate", "config": config}
    )
    assert config == sent


async def test_a_proposal_is_not_logged_as_a_broken_automation(
    component_ws: MockHAClientWebSocket, caplog: pytest.LogCaptureFixture
) -> None:
    """Checking a draft must not leave an error in the user's log saying an
    automation was disabled."""
    with caplog.at_level(logging.WARNING):
        await _command(component_ws, action="validate", config=_BAD_TRIGGER)
    assert not [
        r for r in caplog.records if r.name.startswith(automation_core.__name__)
    ]


async def test_config_core_cannot_even_name_is_a_schema_failure(
    component_ws: MockHAClientWebSocket,
) -> None:
    # Core's fallback id/alias/description check raises even when told not to.
    got = await _command(component_ws, action="validate", config={"alias": ["x"]})
    assert got["status"] == "failed_schema"
    assert "alias" in got["error"]


async def test_public_validator_stands_in_for_a_missing_private_one(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The public validator raises on every failure instead of reporting it.
    with pytest.raises(probatio.Invalid) as raised:
        await automation_core_config.async_validate_config_item(hass, "", {})
    error = raised.value
    # A Core build with only the public validator; this Core's public one
    # calls the private one, so it raises what it raised above.
    monkeypatch.delattr(automation_core_config, "_async_validate_config_item")
    public = AsyncMock(side_effect=error)
    monkeypatch.setattr(automation_core_config, "async_validate_config_item", public)

    got = await _command(component_ws, action="validate", config={})

    assert got == {"status": "failed_schema", "error": str(error)}
    public.assert_awaited_once()


async def test_core_without_a_validator_is_unavailable(
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name in ("_async_validate_config_item", "async_validate_config_item"):
        monkeypatch.delattr(automation_core_config, name)
    got = await _command(component_ws, action="validate", config={})
    assert got["status"] == "unavailable"
    assert not _bridge_failed(caplog)


async def test_unexpected_core_failure_is_unavailable_not_an_error(
    component_ws: MockHAClientWebSocket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure Core cannot be made to raise here.
    monkeypatch.setattr(
        automation_core_config,
        "_async_validate_config_item",
        AsyncMock(side_effect=RuntimeError("blueprint store not loaded")),
    )
    got = await _command(component_ws, action="validate", config={})
    assert got == {"status": "unavailable", "error": "blueprint store not loaded"}


# --- describe -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "core_descriptions"),
    [
        ("trigger", trigger.async_get_all_descriptions),
        ("condition", condition.async_get_all_descriptions),
    ],
)
async def test_descriptions_are_cores_own(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    kind: str,
    core_descriptions: Any,
) -> None:
    for domain in ("homeassistant", "light"):
        assert await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
    expected = await core_descriptions(hass)
    assert expected

    got = await _command(component_ws, action="describe", kind=kind)

    assert got == {"descriptions": expected}


async def test_core_without_descriptions_is_unavailable(
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delattr(condition, "async_get_all_descriptions")
    got = await _command(component_ws, action="describe", kind="condition")
    assert got["status"] == "unavailable"
    assert not _bridge_failed(caplog)


# --- lookup ---------------------------------------------------------------------


async def test_lookup_by_entity_id_returns_cores_registry_row(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The list row lacked the aliases and capabilities the fallback's
    config/entity_registry/get row carries, so the field changed shape with
    the route."""
    await _load(hass, _MORNING)
    row = await ws_result(component_ws, "config/entity_registry/get", entity_id=MORNING)

    got = await _command(component_ws, action="lookup", entity_id=MORNING)

    assert got["found"] is True
    assert got["entity"] == row


async def test_lookup_by_config_id_finds_the_automation_not_another_platform(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_registry.async_get_or_create(
        "light", "hue", "1700", suggested_object_id="porch"
    )
    await _load(hass, _MORNING)
    got = await _command(component_ws, action="lookup", unique_id="1700")
    assert got["entity"]["entity_id"] == MORNING


@pytest.mark.parametrize("entity_id", ["automation.gone", "light.porch"])
async def test_lookup_without_an_automation_row_is_not_found(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    entity_id: str,
) -> None:
    entity_registry.async_get_or_create(
        "light", "hue", "1700", suggested_object_id="porch"
    )
    await _load(hass, _MORNING)
    got = await _command(component_ws, action="lookup", entity_id=entity_id)
    assert got == {
        "found": False,
        "entity": None,
        "loaded": False,
        "validation": None,
        "references": None,
    }


async def test_lookup_reports_the_validation_issue_core_filed(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Core keeps an unavailable placeholder entity for a failed automation,
    whose reference readers answer empty sets; it was counted as loaded, so
    a failed blueprint automation read as one using no blueprint."""
    await _load(hass, _BAD_TRIGGER)
    issue = issue_registry.async_get_issue(
        "automation", f"{MORNING}_validation_failed_triggers"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None

    got = await _command(component_ws, action="lookup", entity_id=MORNING)

    assert got["found"] is True
    assert got["validation"] == {
        "status": "failed_triggers",
        "error": issue.translation_placeholders["error"],
    }
    assert got["loaded"] is False
    assert got["references"] is None


async def test_issue_of_an_automation_whose_id_extends_this_one_is_ignored(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Read by id prefix, the other automation's issue was reported as this
    one's failure."""
    other = "automation.morning_validation_x"
    await _load(
        hass,
        _MORNING,
        {"id": "1701", "alias": "morning validation x"},
    )
    assert issue_registry.async_get_issue(
        "automation", f"{other}_validation_failed_schema"
    )

    got = await _command(component_ws, action="lookup", entity_id=MORNING)

    assert got["found"] is True
    assert got["validation"] is None


@pytest.mark.parametrize("load_registries", [False])
async def test_stored_issue_from_an_earlier_run_is_ignored(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Core keeps a non-persistent issue across a restart only as an inactive
    record; reading it as current reported an automation fixed since then as
    still failing."""
    # The record Core saved for the issue before the restart.
    hass_storage[ir.STORAGE_KEY] = {
        "version": ir.STORAGE_VERSION_MAJOR,
        "minor_version": ir.STORAGE_VERSION_MINOR,
        "data": {
            "issues": [
                {
                    "config_entry_id": None,
                    "created": "2026-01-01T00:00:00+00:00",
                    "dismissed_version": None,
                    "domain": "automation",
                    "is_persistent": False,
                    "issue_id": f"{MORNING}_validation_failed_actions",
                }
            ]
        },
    }
    await ir.async_load(hass)
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    er.async_get(hass).async_get_or_create(
        "automation", "automation", "1700", suggested_object_id="morning"
    )
    stored = ir.async_get(hass).async_get_issue(
        "automation", f"{MORNING}_validation_failed_actions"
    )
    assert stored is not None

    got = await automation_config.async_automation_config(
        hass, {"action": "lookup", "entity_id": MORNING}
    )

    assert got["found"] is True
    assert got["validation"] is None


async def test_lookup_carries_the_references_core_reads(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _load(hass, _MORNING)
    got = await _command(component_ws, action="lookup", entity_id=MORNING)
    assert got["loaded"] is True
    assert got["references"] == {
        "entities": ["light.hall", "light.porch"],
        "devices": ["d1"],
        "areas": [],
        "floors": ["f1"],
        "labels": ["l1", "l2"],
        "blueprint": _BLUEPRINT_PATH,
    }


async def test_references_are_absent_while_the_automation_is_not_loaded(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """The server could not tell a disabled automation from one that
    references nothing; ``loaded`` lets it say why."""
    await _load(hass, _MORNING)
    entity_registry.async_update_entity(
        MORNING, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert hass.states.get(MORNING) is None

    got = await _command(component_ws, action="lookup", entity_id=MORNING)

    assert got["found"] is True
    assert got["loaded"] is False
    assert got["references"] is None


@pytest.mark.parametrize(
    "msg",
    [
        {"action": "validate"},
        {"action": "describe"},
        {"action": "lookup"},
        {"action": "lookup", "entity_id": ""},
    ],
    ids=[
        "validate-without-config",
        "describe-without-kind",
        "lookup-without-a-key",
        "lookup-empty-id",
    ],
)
async def test_request_without_its_input_is_refused_not_answered(
    hass: HomeAssistant, msg: dict[str, Any]
) -> None:
    """Answered, a validate without a config or a describe without a kind
    would read as Core's verdict on nothing, and a lookup without a key or of
    ``""`` as an automation that does not exist."""
    with pytest.raises(HomeAssistantError):
        await automation_config.async_automation_config(hass, msg)


async def test_core_missing_a_lookup_part_is_unavailable(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _load(hass, _MORNING)
    # A Core build from before floors were referenced.
    monkeypatch.delattr(automation_core, "floors_in_automation")
    got = await _command(component_ws, action="lookup", entity_id=MORNING)
    assert got["status"] == "unavailable"
    assert not _bridge_failed(caplog)


async def test_component_key_is_read_from_the_package_on_older_core(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core 2026.10 defines DATA_COMPONENT in the automation package and only
    2026.11 moves it to automation.const; reading const alone left the lookup
    "unavailable" on every 2026.10 install."""
    await _load(hass, _MORNING)
    monkeypatch.delattr(automation_core_const, "DATA_COMPONENT")
    got = await _command(component_ws, action="lookup", entity_id=MORNING)
    assert got["loaded"] is True


# --- registration ---------------------------------------------------------------


async def test_command_is_admin_gated(
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
) -> None:
    frame = {"action": "validate", "config": {"triggers": [], "actions": []}}
    refused = await ws_call(
        component_ws_read_only, automation_config.WS_AUTOMATION_CONFIG, **frame
    )
    assert refused["error"]["code"] == "unauthorized"
    answered = await ws_call(
        component_ws, automation_config.WS_AUTOMATION_CONFIG, **frame
    )
    assert answered["success"], answered
