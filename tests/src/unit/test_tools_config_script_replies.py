"""What a script write replies on a real ``hass``: Core's refusal with the
hints it supports, and whether the write created or updated the script, as the
automation tool replies."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from ._scripts_core import BODY, KEY, WRITE_MODES, _stored, _write_arguments
from ._scripts_core import tools as tools
from ._tool_errors import rejected_call_body


async def test_config_without_sequence_or_blueprint_gets_cores_refusal(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    """Core's config API judges the script; the tool passes its reason on."""
    body = await rejected_call_body(
        tools.ha_config_set_script(script_id=KEY, config={"alias": "Test Script"})
    )

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "required key not provided" in body["error"]["message"]
    assert body["failed_section"] == "schema"
    assert not any("blueprint" in s.lower() for s in body["error"]["suggestions"])
    assert await _stored(hass) == {KEY: BODY}


@pytest.mark.parametrize("mode", WRITE_MODES)
async def test_a_refused_action_names_the_sequence_as_what_failed(
    hass: HomeAssistant, tools: ConfigScriptTools, mode: str
) -> None:
    """Core's 400 names no section; Core's ``validate_config`` on the sequence
    does, in both write modes."""
    arguments = (
        {"config": {"alias": "Morning", "sequence": [{"foo": 1}]}}
        if mode == "full_config"
        else {
            "python_transform": "config['sequence'] = [{'foo': 1}]",
            "config_hash": (await tools.ha_config_get_script(script_id=KEY))[
                "config_hash"
            ],
        }
    )

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id=KEY, **arguments)
    )

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["failed_section"] == "sequence"
    assert await _stored(hass) == {KEY: BODY}


async def test_an_unknown_trigger_in_a_script_names_the_closest_registered_ones(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    """Core's text alone names only the key it refused; the registered keys
    nearest to it come from the platforms Core describes, as for automations."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "schedule", {})
    await hass.async_block_till_done()
    config = {
        "alias": "Wait",
        "sequence": [{"wait_for_trigger": [{"trigger": "schedule.turned_on"}]}],
    }

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id="waiter", config=config)
    )

    assert "schedule.block_started" in body["closest_matches"]
    assert await _stored(hass) == {KEY: BODY}


async def test_a_missing_blueprint_points_to_the_blueprint_list(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    config = {"alias": "B", "use_blueprint": {"path": "nope.yaml", "input": {}}}

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id="from_bp", config=config)
    )

    assert body["failed_section"] == "blueprint"
    assert any(
        "ha_manage_blueprints(action='list', domain='script')" in s
        for s in body["error"]["suggestions"]
    )


@pytest.mark.parametrize(
    ("script_id", "mode", "operation"),
    [
        ("new_script", "full_config", "created"),
        (KEY, "full_config", "updated"),
        (f"script.{KEY}", "full_config", "updated"),
        (KEY, "hash_checked", "updated"),
        (KEY, "transform", "updated"),
    ],
)
async def test_a_write_says_whether_it_created_or_updated_the_script(
    tools: ConfigScriptTools, script_id: str, mode: str, operation: str
) -> None:
    """As the automation tool does (#2903): ``operation`` from whether Core held
    a config under the key before the write, beside the entity and the hash
    of the config Core stored."""
    arguments = await _write_arguments(tools, mode, script_id)
    if mode == "hash_checked":
        current = await tools.ha_config_get_script(script_id=script_id)
        arguments["config_hash"] = current["config_hash"]

    result = await tools.ha_config_set_script(script_id=script_id, **arguments)

    key = script_id.removeprefix("script.")
    assert result["operation"] == operation
    assert result["entity_id"] == f"script.{key}"
    stored = await tools.ha_config_get_script(script_id=key)
    assert result["config_hash"] == stored["config_hash"]


async def test_a_failed_read_back_keeps_the_write_successful(
    hass: HomeAssistant, tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """Core stored the script; only its hash is missing, and a warning says so.
    A transform reads the script before its write, so it is not driven here."""
    arguments = await _write_arguments(tools, "full_config", KEY)
    with patch.object(
        core_client,
        "get_script_config",
        side_effect=HomeAssistantConnectionError("offline"),
    ):
        result = await tools.ha_config_set_script(script_id=KEY, **arguments)

    assert result["success"] is True
    assert "config_hash" not in result
    assert any("re-read it for a config_hash" in w for w in result["warnings"])
    assert (await _stored(hass))[KEY] != BODY
