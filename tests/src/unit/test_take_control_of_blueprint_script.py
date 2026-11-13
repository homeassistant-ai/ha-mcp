"""``ha_config_set_script(take_control_of_blueprint=True)`` on real Core (#2329).

Sibling of the automation coverage: script blueprints get the same one-shot,
which is what makes the blueprint tool's in-use refusal name a call that a
script consumer can actually run. Core holds the script in ``scripts.yaml``
and the blueprint under ``blueprints/script``.
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
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._tool_errors import error_of

_SID = "notify_on_door"
_PATH = "b3/notification.yaml"
_INPUT = {"message": "hello"}

# Only the script domain holds this blueprint, so rendering it proves the
# tool asked Core for a script blueprint.
_BLUEPRINT = """\
blueprint:
  name: Notification
  domain: script
  input:
    message:
      selector: {text: {}}
mode: queued
sequence:
  - action: persistent_notification.create
    data:
      message: !input message
"""

_BLUEPRINT_SCRIPT: dict[str, Any] = {
    "alias": "Notify on door",
    "description": "written by a human, must survive",
    "use_blueprint": {"path": _PATH, "input": _INPUT},
}


async def _scripts(hass: HomeAssistant, script: dict[str, Any]) -> None:
    await write_config_files(
        hass,
        {
            f"blueprints/script/{_PATH}": _BLUEPRINT,
            "scripts.yaml": dump({_SID: script}),
            "configuration.yaml": "script: !include scripts.yaml\n",
        },
    )
    await setup_from_configuration(hass, "script")


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> ConfigScriptTools:
    """The script tools over a blueprint script."""
    await _scripts(hass, _BLUEPRINT_SCRIPT)
    return ConfigScriptTools(core_client)


async def _stored(client: HomeAssistantClient) -> dict[str, Any]:
    """The script as Core's config API holds it now."""
    config: dict[str, Any] = (await client.get_script_config(_SID))["config"]
    return config


def _spy_upsert(client: HomeAssistantClient) -> Any:
    return patch.object(
        client, "upsert_script_config", wraps=client.upsert_script_config
    )


async def test_saves_cores_rendering_in_place_of_the_blueprint_link(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """The stored script is Core's rendering of its own inputs, including the
    blueprint's ``mode``, and no longer references the blueprint."""
    rendered = await core_client.send_websocket_message(
        {
            "type": "blueprint/substitute",
            "domain": "script",
            "path": _PATH,
            "input": _INPUT,
        }
    )
    assert rendered["success"], rendered

    with _spy_upsert(core_client) as upsert:
        result = await tools.ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, wait=False
        )

    assert result["success"] is True
    upsert.assert_awaited_once()
    stored = await _stored(core_client)
    assert "use_blueprint" not in stored
    assert stored["mode"] == "queued"
    expected = rendered["result"]["substituted_config"]
    assert {key: stored[key] for key in expected} == expected


async def test_labels_survive_the_conversion(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """The script keeps its own name rather than taking the blueprint's."""
    await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    stored = await _stored(core_client)
    assert stored["alias"] == "Notify on door"
    assert stored["description"] == "written by a human, must survive"


async def test_response_names_the_blueprint(tools: ConfigScriptTools) -> None:
    result = await tools.ha_config_set_script(
        script_id=_SID, take_control_of_blueprint=True, wait=False
    )

    assert result["took_control_of_blueprint"] == _PATH


async def test_refuses_a_script_that_uses_no_blueprint(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _scripts(hass, {"alias": "Hand written", "sequence": [{"delay": 1}]})

    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await ConfigScriptTools(core_client).ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, wait=False
        )

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    upsert.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"config": {"alias": "x", "sequence": []}},
        {"python_transform": "config['mode'] = 'single'", "config_hash": "h"},
    ],
    ids=["with_config", "with_python_transform"],
)
async def test_refuses_to_combine_with_another_write_mode(
    tools: ConfigScriptTools,
    core_client: HomeAssistantClient,
    extra: dict[str, Any],
) -> None:
    with _spy_upsert(core_client) as upsert, pytest.raises(ToolError) as exc:
        await tools.ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, **extra
        )

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    upsert.assert_not_awaited()


async def test_a_concurrent_edit_between_read_and_write_is_caught(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """Take control locks the write against the config it actually read.

    Same guarantee as the automation path: the tool does the read, so the
    tool supplies the hash the caller could not. Here another writer renames
    the script in ``scripts.yaml`` after the tool's first read.
    """
    load_yaml = config_view.load_yaml
    edited = {**_BLUEPRINT_SCRIPT, "alias": "renamed by someone else"}
    reads: list[str] = []

    def read_after_another_writer(path: str) -> Any:
        if path.endswith("scripts.yaml"):
            reads.append(path)
            if len(reads) == 2:
                Path(path).write_text(dump({_SID: edited}))
        return load_yaml(path)

    with (
        patch.object(config_view, "load_yaml", read_after_another_writer),
        _spy_upsert(core_client) as upsert,
        pytest.raises(ToolError) as exc,
    ):
        await tools.ha_config_set_script(
            script_id=_SID, take_control_of_blueprint=True, wait=False
        )

    assert "conflict" in error_of(exc)["message"]
    upsert.assert_not_awaited()
    assert (await _stored(core_client))["alias"] == "renamed by someone else"
