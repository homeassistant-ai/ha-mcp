"""``ha_config_set_automation(take_control_of_blueprint=True)`` (#2329).

The UI's "Take control": render the blueprint an automation is built on with
that automation's own current inputs, then save the rendering over it so the
automation owns its config. These pin the parts that make it a conversion
rather than an ordinary write -- what carries over, what the blueprint's
output is allowed to win, and the refusals that keep it from firing on
something it cannot convert.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools import tools_config_automations
from ha_mcp.tools.auto_backup import automation_backup_target
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

_ID = "automation.motion_light_kitchen"
_PATH = "homeassistant/motion_light.yaml"

_BLUEPRINT_CONFIG: dict[str, Any] = {
    "id": "abc123unique",
    "alias": "Motion Light Kitchen",
    "description": "written by a human, must survive",
    "use_blueprint": {
        "path": _PATH,
        "input": {"motion_entity": "binary_sensor.kitchen_motion"},
    },
}

# What core's blueprint/substitute hands back: a complete standalone config.
_SUBSTITUTED: dict[str, Any] = {
    "alias": "Motion Light (blueprint default name)",
    "mode": "restart",
    "triggers": [
        {"trigger": "state", "entity_id": "binary_sensor.kitchen_motion", "to": "on"}
    ],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
}


def _error(exc: ToolError) -> dict[str, Any]:
    import json

    payload = json.loads(str(exc))
    return payload.get("error", payload)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.base_url = None
    client.token = None
    client.get_automation_config = AsyncMock(return_value=dict(_BLUEPRINT_CONFIG))
    client.get_states = AsyncMock(
        return_value=[{"entity_id": _ID, "attributes": {"id": "abc123unique"}}]
    )
    client.get_services = AsyncMock(return_value={})
    client.upsert_automation_config = AsyncMock(
        return_value={
            "unique_id": "abc123unique",
            "entity_id": _ID,
            "result": "ok",
            "operation": "updated",
        }
    )

    async def ws(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("type") == "blueprint/substitute":
            return {
                "success": True,
                "result": {"substituted_config": dict(_SUBSTITUTED)},
            }
        return {"success": True, "result": {"categories": {}}}

    client.send_websocket_message = AsyncMock(side_effect=ws)
    return client


@pytest.fixture
def tools(mock_client):
    return AutomationConfigTools(mock_client)


def _written(mock_client) -> dict[str, Any]:
    """The config actually handed to the upsert."""
    return mock_client.upsert_automation_config.call_args[0][0]


async def test_replaces_the_blueprint_link_with_the_rendered_config(tools, mock_client):
    """The saved config is the blueprint's output, no longer a reference to it."""
    result = await tools.ha_config_set_automation(
        identifier=_ID, take_control_of_blueprint=True, wait=False
    )

    assert result["success"] is True
    written = _written(mock_client)
    assert "use_blueprint" not in written
    assert written["triggers"] == _SUBSTITUTED["triggers"]
    assert written["actions"] == _SUBSTITUTED["actions"]


async def test_identity_and_labels_survive_the_conversion(tools, mock_client):
    """id/alias/description carry over -- the entity keeps its name (#2329).

    The blueprint's own ``alias`` would otherwise rename the user's automation
    to the blueprint's default, which is what the frontend guards against.
    """
    await tools.ha_config_set_automation(
        identifier=_ID, take_control_of_blueprint=True, wait=False
    )

    written = _written(mock_client)
    assert written["id"] == "abc123unique"
    assert written["alias"] == "Motion Light Kitchen"
    assert written["description"] == "written by a human, must survive"


async def test_mode_comes_from_the_blueprint_not_the_original(tools, mock_client):
    """Everything except identity is the rendering's to decide."""
    await tools.ha_config_set_automation(
        identifier=_ID, take_control_of_blueprint=True, wait=False
    )

    assert _written(mock_client)["mode"] == "restart"


async def test_substitute_is_asked_for_the_automations_own_inputs(tools, mock_client):
    """Take control renders with what the automation is configured with today."""
    await tools.ha_config_set_automation(
        identifier=_ID, take_control_of_blueprint=True, wait=False
    )

    frames = [
        c.args[0]
        for c in mock_client.send_websocket_message.call_args_list
        if c.args[0].get("type") == "blueprint/substitute"
    ]
    assert len(frames) == 1
    assert frames[0]["domain"] == "automation"
    assert frames[0]["path"] == _PATH
    assert frames[0]["input"] == {"motion_entity": "binary_sensor.kitchen_motion"}


async def test_response_names_the_blueprint_it_detached_from(tools):
    """The caller is told what the automation is no longer linked to."""
    result = await tools.ha_config_set_automation(
        identifier=_ID, take_control_of_blueprint=True, wait=False
    )

    assert result["took_control_of_blueprint"] == _PATH


async def test_refuses_an_automation_that_uses_no_blueprint(tools, mock_client):
    """Nothing to take control of -- and nothing is written."""
    mock_client.get_automation_config = AsyncMock(
        return_value={
            "id": "abc123unique",
            "alias": "Hand written",
            "triggers": [{"trigger": "time", "at": "07:00:00"}],
            "actions": [{"action": "light.turn_on"}],
        }
    )

    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(
            identifier=_ID, take_control_of_blueprint=True, wait=False
        )

    assert _error(exc.value)["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.upsert_automation_config.assert_not_called()


async def test_requires_an_identifier(tools, mock_client):
    """There is no automation to convert without one."""
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(take_control_of_blueprint=True)

    assert _error(exc.value)["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.upsert_automation_config.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"config": {"alias": "x", "triggers": [], "actions": []}},
        {"python_transform": "config['mode'] = 'single'", "config_hash": "h"},
    ],
    ids=["with_config", "with_python_transform"],
)
async def test_refuses_to_combine_with_another_write_mode(tools, mock_client, extra):
    """Take control replaces the whole config, so pairing it is a caller mistake."""
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(
            identifier=_ID, take_control_of_blueprint=True, **extra
        )

    assert _error(exc.value)["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.upsert_automation_config.assert_not_called()


async def test_a_missing_input_is_reported_as_a_validation_failure(tools, mock_client):
    """Core answers every substitute failure alike; the text is the discriminator."""

    async def ws(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("type") == "blueprint/substitute":
            return {"success": False, "error": {"message": "Missing input: light"}}
        return {"success": True, "result": {"categories": {}}}

    mock_client.send_websocket_message = AsyncMock(side_effect=ws)

    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_automation(
            identifier=_ID, take_control_of_blueprint=True, wait=False
        )

    assert _error(exc.value)["code"] == "VALIDATION_FAILED"
    mock_client.upsert_automation_config.assert_not_called()


def test_the_pre_conversion_config_is_what_gets_snapshotted():
    """Take control is one-way, so the backup must hold the blueprint version.

    ``config`` is None on this path, so the target can only come from
    ``identifier`` -- a regression to config-only resolution would silently
    skip the capture and leave the conversion unrecoverable.
    """
    target = automation_backup_target(
        {"identifier": _ID, "config": None, "take_control_of_blueprint": True}
    )

    assert target == _ID


async def test_take_control_does_not_pretend_to_free_the_blueprint(
    tools, mock_client, monkeypatch: pytest.MonkeyPatch
):
    """No usage-index lookup is made, because the conversion cannot clear it.

    Verified live against Home Assistant 2026.9: after taking control the
    blueprint still counts the automation as a user and deleting it is still
    refused -- an automation reload does not change that. A wait here would
    always time out, charging every caller for a guarantee that cannot hold.
    """

    async def _ok(*_args: Any, **_kw: Any) -> Any:
        return True

    # Exercise the DEFAULT wait=True path -- that is where a usage-index wait
    # would have lived.
    monkeypatch.setattr(tools_config_automations, "wait_for_entity_registered", _ok)

    await tools.ha_config_set_automation(identifier=_ID, take_control_of_blueprint=True)

    assert not [
        c
        for c in mock_client.send_websocket_message.call_args_list
        if c.args[0].get("type") == "search/related"
    ]
