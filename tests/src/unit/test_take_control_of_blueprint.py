"""``ha_config_set_automation(take_control_of_blueprint=True)`` on real Core (#2329).

The UI's "Take control": render the blueprint an automation is built on with
that automation's own current inputs, then save the rendering over it so the
automation owns its config. Core holds the automation in ``automations.yaml``
and the blueprint under ``blueprints/automation``, renders it with its own
``blueprint/substitute`` and stores what the tool writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.config import view as config_view
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import dump

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._tool_errors import error_of

_ID = "automation.motion_light_kitchen"
_PATH = "b3/motion_light.yaml"
_INPUT = {"motion_entity": "binary_sensor.kitchen_motion", "light": "light.kitchen"}

_BLUEPRINT = """\
blueprint:
  name: Motion light
  domain: automation
  input:
    motion_entity:
      selector: {entity: {domain: binary_sensor}}
    light:
      selector: {entity: {domain: light}}
mode: restart
triggers:
  - trigger: state
    entity_id: !input motion_entity
    to: "on"
actions:
  - action: light.turn_on
    target:
      entity_id: !input light
"""

_BLUEPRINT_AUTOMATION: dict[str, Any] = {
    "id": "abc123unique",
    "alias": "Motion Light Kitchen",
    "description": "written by a human, must survive",
    "use_blueprint": {"path": _PATH, "input": _INPUT},
}


async def _automations(hass: HomeAssistant, *automations: dict[str, Any]) -> None:
    await write_config_files(
        hass,
        {
            f"blueprints/automation/{_PATH}": _BLUEPRINT,
            "automations.yaml": dump(list(automations)),
            "configuration.yaml": "automation: !include automations.yaml\n",
        },
    )
    await setup_from_configuration(hass, "automation")


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AutomationConfigTools:
    """The automation tools over a blueprint automation."""
    await _automations(hass, _BLUEPRINT_AUTOMATION)
    return AutomationConfigTools(core_client)


async def _stored(client: HomeAssistantClient) -> dict[str, Any]:
    """The automation as Core's config API holds it now."""
    return await client.get_automation_config("abc123unique")


def _spy_upsert(client: HomeAssistantClient) -> Any:
    return patch.object(
        client, "upsert_automation_config", wraps=client.upsert_automation_config
    )


async def test_saves_cores_rendering_in_place_of_the_blueprint_link(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """Everything but identity is the rendering's, including the blueprint's
    ``mode``; the stored config no longer references the blueprint."""
    rendered = await core_client.send_websocket_message(
        {
            "type": "blueprint/substitute",
            "domain": "automation",
            "path": _PATH,
            "input": _INPUT,
        }
    )
    assert rendered["success"], rendered

    with _spy_upsert(core_client) as upsert:
        result = await tools.ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True, wait=False
        )

    assert result["success"] is True
    upsert.assert_awaited_once()
    stored = await _stored(core_client)
    assert "use_blueprint" not in stored
    assert stored["mode"] == "restart"
    expected = rendered["result"]["substituted_config"]
    assert {key: stored[key] for key in expected} == expected


async def test_identity_and_labels_survive_the_conversion(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """id/alias/description carry over, so the entity keeps its id and name."""
    await tools.ha_config_set_automation(
        entity_id=_ID, take_control_of_blueprint=True, wait=False
    )

    stored = await _stored(core_client)
    assert stored["id"] == "abc123unique"
    assert stored["alias"] == "Motion Light Kitchen"
    assert stored["description"] == "written by a human, must survive"


async def test_response_names_the_blueprint_it_detached_from(
    tools: AutomationConfigTools,
) -> None:
    result = await tools.ha_config_set_automation(
        entity_id=_ID, take_control_of_blueprint=True, wait=False
    )

    assert result["took_control_of_blueprint"] == _PATH


async def test_refuses_an_automation_that_uses_no_blueprint(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Nothing to take control of -- and nothing is written."""
    hand_written = {
        "id": "abc123unique",
        "alias": "Motion Light Kitchen",
        "triggers": [{"trigger": "event", "event_type": "kitchen_motion"}],
        "actions": [{"action": "light.turn_on"}],
    }
    await _automations(hass, hand_written)

    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await AutomationConfigTools(core_client).ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True, wait=False
        )

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    upsert.assert_not_awaited()


async def test_requires_an_entity_id(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """There is no automation to convert without one."""
    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(take_control_of_blueprint=True)

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    upsert.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"config": {"alias": "x", "triggers": [], "actions": []}},
        {"python_transform": "config['mode'] = 'single'", "config_hash": "h"},
    ],
    ids=["with_config", "with_python_transform"],
)
async def test_refuses_to_combine_with_another_write_mode(
    tools: AutomationConfigTools,
    core_client: HomeAssistantClient,
    extra: dict[str, Any],
) -> None:
    """Take control replaces the whole config, so pairing it is a caller mistake."""
    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True, **extra
        )

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    upsert.assert_not_awaited()


async def test_a_missing_input_is_reported_as_a_validation_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers every substitute failure alike; its text names the missing input."""
    missing_light = {
        **_BLUEPRINT_AUTOMATION,
        "use_blueprint": {
            "path": _PATH,
            "input": {"motion_entity": "binary_sensor.kitchen_motion"},
        },
    }
    await _automations(hass, missing_light)

    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await AutomationConfigTools(core_client).ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True, wait=False
        )

    assert error_of(exc)["code"] == "VALIDATION_FAILED"
    upsert.assert_not_awaited()


async def test_the_blueprint_version_is_what_gets_snapshotted(
    tools: AutomationConfigTools,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Take control is one-way, so the backup must hold the blueprint version.

    No ``config`` is passed on this path, so the snapshot target can only come
    from ``entity_id``.
    """
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()

    await tools.ha_config_set_automation(
        entity_id=_ID, take_control_of_blueprint=True, wait=False
    )

    manager = get_backup_manager(core_client, get_global_settings())
    [row] = manager.list_snapshots(domain="automation")
    snapshot = manager.read_snapshot(row["name"])
    assert snapshot["config"]["use_blueprint"] == _BLUEPRINT_AUTOMATION["use_blueprint"]


async def test_take_control_does_not_wait_on_the_blueprint_usage_index(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """No usage-index lookup is made, because the conversion cannot clear it.

    Verified live against Home Assistant 2026.9: after taking control the
    blueprint still counts the automation as a user and deleting it is still
    refused -- an automation reload does not change that. A wait here would
    always time out, charging every caller for a guarantee that cannot hold.
    The default ``wait=True`` is where such a wait would live.
    """
    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as sent:
        result = await tools.ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True
        )

    assert result["success"] is True
    assert not [
        call
        for call in sent.await_args_list
        if call.args[0]["type"] == "search/related"
    ]


# --------------------------------------------- optimistic locking on its own read


async def test_a_concurrent_edit_between_read_and_write_is_caught(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """Take control locks the write against the config it actually read.

    It is a read-modify-write the TOOL performs, so the caller has no config
    to lock against and cannot supply a hash. Here another writer renames the
    automation in ``automations.yaml`` after the tool's first read; without the
    tool's own hash that edit would be silently overwritten.
    """
    load_yaml = config_view.load_yaml
    edited = {**_BLUEPRINT_AUTOMATION, "alias": "renamed by someone else"}
    reads: list[str] = []

    def read_after_another_writer(path: str) -> Any:
        if path.endswith("automations.yaml"):
            reads.append(path)
            if len(reads) == 2:
                Path(path).write_text(dump([edited]))
        return load_yaml(path)

    with (
        patch.object(config_view, "load_yaml", read_after_another_writer),
        _spy_upsert(core_client) as upsert,
        pytest.raises(ToolError) as exc,
    ):
        await tools.ha_config_set_automation(
            entity_id=_ID, take_control_of_blueprint=True, wait=False
        )

    assert "conflict" in error_of(exc)["message"]
    upsert.assert_not_awaited()
    assert (await _stored(core_client))["alias"] == "renamed by someone else"


async def test_an_explicit_caller_hash_still_wins(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """A caller locking against a config THEY read keeps that guarantee."""
    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(
            entity_id=_ID,
            take_control_of_blueprint=True,
            config_hash="a-hash-that-does-not-match",
            wait=False,
        )

    assert "conflict" in error_of(exc)["message"]
    upsert.assert_not_awaited()
