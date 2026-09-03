"""``ha_config_set_script(take_control_of_blueprint=True)`` (#2329).

Sibling of the automation coverage: script blueprints get the same one-shot,
which is what makes the blueprint tool's in-use refusal name a call that a
script consumer can actually run.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

_SID = "notify_on_door"
_PATH = "someone/notification.yaml"

_BLUEPRINT_CONFIG: dict[str, Any] = {
    "alias": "Notify on door",
    "description": "written by a human, must survive",
    "use_blueprint": {"path": _PATH, "input": {"message": "hello"}},
}

_SUBSTITUTED: dict[str, Any] = {
    "alias": "Blueprint default name",
    "mode": "queued",
    "sequence": [{"action": "notify.notify", "data": {"message": "hello"}}],
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
    client.get_script_config = AsyncMock(
        return_value={"script_id": _SID, "config": dict(_BLUEPRINT_CONFIG)}
    )
    client.get_states = AsyncMock(return_value=[])
    client.get_services = AsyncMock(return_value={})
    client.upsert_script_config = AsyncMock(
        return_value={"script_id": _SID, "result": "ok", "operation": "updated"}
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
    return ConfigScriptTools(mock_client)


def _written(mock_client) -> dict[str, Any]:
    return mock_client.upsert_script_config.call_args[0][0]


async def test_replaces_the_blueprint_link_with_the_rendered_sequence(
    tools, mock_client
):
    """The saved config is the blueprint's output, not a reference to it."""
    result = await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    assert result["success"] is True
    written = _written(mock_client)
    assert "use_blueprint" not in written
    assert written["sequence"] == _SUBSTITUTED["sequence"]


async def test_labels_survive_the_conversion(tools, mock_client):
    """The script keeps its own name rather than taking the blueprint's."""
    await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    written = _written(mock_client)
    assert written["alias"] == "Notify on door"
    assert written["description"] == "written by a human, must survive"
    assert written["mode"] == "queued"


async def test_substitute_uses_the_script_domain_and_its_own_inputs(tools, mock_client):
    """A script blueprint renders through the script domain, not automation."""
    await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    frames = [
        c.args[0]
        for c in mock_client.send_websocket_message.call_args_list
        if c.args[0].get("type") == "blueprint/substitute"
    ]
    assert len(frames) == 1
    assert frames[0]["domain"] == "script"
    assert frames[0]["path"] == _PATH
    assert frames[0]["input"] == {"message": "hello"}


async def test_response_names_the_blueprint(tools):
    result = await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    assert result["took_control_of_blueprint"] == _PATH


async def test_refuses_a_script_that_uses_no_blueprint(tools, mock_client):
    mock_client.get_script_config = AsyncMock(
        return_value={
            "script_id": _SID,
            "config": {"alias": "Hand written", "sequence": [{"delay": 1}]},
        }
    )

    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, wait=False
        )

    assert _error(exc.value)["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.upsert_script_config.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"config": {"alias": "x", "sequence": []}},
        {"python_transform": "config['mode'] = 'single'", "config_hash": "h"},
    ],
    ids=["with_config", "with_python_transform"],
)
async def test_refuses_to_combine_with_another_write_mode(tools, mock_client, extra):
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, **extra
        )

    assert _error(exc.value)["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.upsert_script_config.assert_not_called()
