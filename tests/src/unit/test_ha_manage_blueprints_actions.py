"""``ha_manage_blueprints`` ``list``, ``substitute``, ``get`` and ``delete`` on real Core (#2329).

Blueprints are files Core loads from the test's config directory; automations
and scripts using them are set up from YAML, so Core's own usage index decides
whether a delete is refused and which consumers ``search/related`` names.
``substitute`` is Core's own rendering and its message-only error taxonomy.
``get`` adds the blueprint's consumers and its YAML from the first tier of the
text ladder that can serve it. ``delete`` covers the ``confirm=True`` gate, the
``RESOURCE_LOCKED`` refusal and its degraded variants, the post-delete
verification and the auto-backup capture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.blueprint.models import DomainBlueprints
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_blueprints import BlueprintTools

from ._blueprint_actions_core import (
    BEDTIME,
    MOTION,
    NO_INPUTS,
    PATH,
    SCRIPT_PATH,
    automation_using,
    enable_auto_backup,
    frames,
    installed_text,
    set_up_core,
)
from ._component_frames import dropped, pooled
from ._tool_errors import rejected_call_body

_SOURCE_URL = "https://example.com/motion.yaml"
_URL_PATH = "example.com/motion.yaml"
_PUBLISHED = MOTION.replace("Motion Light", "Motion Light, as published")
_WITH_SOURCE_URL = MOTION.replace(
    "  domain: automation\n", f"  domain: automation\n  source_url: {_SOURCE_URL}\n", 1
)


async def _substitute(client: HomeAssistantClient, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await BlueprintTools(client).ha_manage_blueprints(
        action="substitute", path=PATH, **params
    )
    return result


async def _get(
    client: HomeAssistantClient, path: str = PATH, **params: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await BlueprintTools(client).ha_manage_blueprints(
        action="get", path=path, **params
    )
    return result


# ------------------------------------------------------------------ substitute


async def test_substitute_returns_cores_standalone_config(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    result = await _substitute(
        core_client, input={"motion_sensor": "binary_sensor.hall"}
    )

    assert result == {
        "success": True,
        "data": {
            "domain": "automation",
            "path": PATH,
            "config": {
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": "binary_sensor.hall",
                        "to": "on",
                    }
                ],
                "actions": [{"action": "light.turn_on"}],
            },
        },
    }
    assert frames(sent, "blueprint/substitute") == [
        {
            "type": "blueprint/substitute",
            "domain": "automation",
            "path": PATH,
            "input": {"motion_sensor": "binary_sensor.hall"},
        }
    ]


async def test_substitute_defaults_input_to_empty_dict(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """An omitted ``input`` still satisfies Core's required key."""
    await set_up_core(hass, {f"automation/{PATH}": NO_INPUTS})

    result = await _substitute(core_client)

    assert result["data"]["config"] == {"actions": [{"action": "light.turn_on"}]}
    assert frames(sent, "blueprint/substitute")[0]["input"] == {}


async def test_substitute_missing_input_is_validation_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    body = await rejected_call_body(_substitute(core_client, input={}))

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "motion_sensor" in body["error"]["message"]


async def test_substitute_unknown_blueprint_is_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass)

    body = await rejected_call_body(_substitute(core_client, input={}))

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_substitute_other_failure_is_service_call_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core refusing the command itself (a path outside the blueprint store) is
    neither a missing input nor a missing blueprint."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="substitute", path="../secrets.yaml", input={}
        )
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"


# ----------------------------------------------------------------------- list


async def test_list_response_shape_unchanged(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    result = await BlueprintTools(core_client).ha_manage_blueprints(
        action="list", domain="automation"
    )

    assert result["success"] is True
    data = result["data"]
    assert (data["domain"], data["count"]) == ("automation", 1)
    assert data["blueprints"][0]["path"] == PATH
    assert data["blueprints"][0]["description"] == "Turn on a light on motion."
    assert frames(sent, "blueprint/list") == [
        {"type": "blueprint/list", "domain": "automation"}
    ]


# ------------------------------------------------------------------------ get


async def test_get_surfaces_yaml_and_its_source(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION}, search=True)

    result = await _get(component_core_client)

    data = result["data"]
    assert data["metadata"]["name"] == "Motion Light"
    assert data["yaml"] == MOTION
    assert data["yaml_source"] == "component"
    assert data["config"]["actions"] == [{"action": "light.turn_on"}]
    # Warnings stay top-level, never nested in data (style guide); none apply here.
    assert "warnings" not in result


async def test_get_warns_when_the_yaml_is_a_source_url_refetch(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without the component the recorded source_url is the last tier: the text
    is the author's current one, not the installed file."""
    aioclient_mock.get(_SOURCE_URL, text=_PUBLISHED)
    await set_up_core(hass, {f"automation/{_URL_PATH}": _WITH_SOURCE_URL}, search=True)

    result = await _get(core_client, path=_URL_PATH)

    assert result["data"]["yaml_source"] == "source_url"
    assert result["data"]["yaml"] == _PUBLISHED
    [warning] = result["warnings"]
    assert _SOURCE_URL in warning
    assert "not read from the installed file" in warning


async def test_get_omits_yaml_keys_when_no_tier_answers(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION}, search=True)

    result = await _get(core_client)

    assert result["success"] is True
    assert result["data"]["metadata"]["name"] == "Motion Light"
    for key in ("yaml", "yaml_source", "config"):
        assert key not in result["data"], key
    assert "warnings" not in result


async def test_get_reports_the_automations_using_the_blueprint(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """The UI's "Show automations using this blueprint", answerable without
    attempting a delete first."""
    await set_up_core(
        hass,
        {f"automation/{PATH}": MOTION},
        automations=[
            automation_using("Porch light", "binary_sensor.porch"),
            automation_using("Hall light", "binary_sensor.hall"),
        ],
        search=True,
    )

    result = await _get(core_client)

    assert result["data"]["used_by"] == [
        "automation.hall_light",
        "automation.porch_light",
    ]
    assert frames(sent, "search/related")[0]["item_type"] == "automation_blueprint"
    assert "warnings" not in result


async def test_get_reports_an_unused_blueprint_as_an_empty_list(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION}, search=True)

    result = await _get(core_client)

    assert result["data"]["used_by"] == []
    assert "warnings" not in result


async def test_get_omits_used_by_when_core_has_no_reference_lookup(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An absent key must never read as "nothing uses this": say so instead."""
    await set_up_core(
        hass,
        {f"automation/{PATH}": MOTION},
        automations=[automation_using("Hall light", "binary_sensor.hall")],
    )

    result = await _get(core_client)

    assert "used_by" not in result["data"]
    assert any("reference lookup" in w for w in result["warnings"])


async def test_get_omits_used_by_when_the_lookup_drops_the_connection(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION}, search=True)
    socket = await pooled(core_client)

    with dropped(hass, "search/related", socket):
        result = await _get(core_client)

    assert result["success"] is True
    assert "used_by" not in result["data"]
    assert any("reference lookup" in w for w in result["warnings"])


async def test_get_used_by_follows_the_script_domain(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await set_up_core(
        hass,
        {f"script/{SCRIPT_PATH}": BEDTIME},
        scripts={
            "bedtime": {
                "use_blueprint": {
                    "path": SCRIPT_PATH,
                    "input": {"light": "light.hall"},
                }
            }
        },
        search=True,
    )

    result = await BlueprintTools(core_client).ha_manage_blueprints(
        action="get", domain="script", path=SCRIPT_PATH
    )

    assert result["data"]["used_by"] == ["script.bedtime"]
    assert frames(sent, "search/related")[0]["item_type"] == "script_blueprint"


# ------------------------------------------------------- parameters and delete


@pytest.mark.parametrize(
    ("action", "missing"),
    [
        ("get", "path"),
        ("import", "url"),
        ("save", "path"),
        ("delete", "path"),
        ("substitute", "path"),
    ],
)
async def test_required_parameter_missing(
    core_client: HomeAssistantClient, sent: AsyncMock, action: str, missing: str
) -> None:
    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(action=action)
    )

    assert body["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert f"'{missing}' is required for action='{action}'" in body["error"]["message"]
    sent.assert_not_called()


async def test_invalid_domain_rejected(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(action="list", domain="light")
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Invalid domain 'light'" in body["error"]["message"]
    sent.assert_not_called()


async def test_delete_requires_confirm(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """No confirm: refused before any frame is sent, and the file stays."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(action="delete", path=PATH)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["error"]["message"] == (
        f"Deletion not confirmed. Set confirm=True to delete blueprint '{PATH}'."
    )
    sent.assert_not_called()
    assert await installed_text(hass, "automation", PATH) == MOTION


async def test_delete_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """The existence pre-check names what is installed instead."""
    await set_up_core(hass, {"automation/other/thing.yaml": MOTION})

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="delete", path=PATH, confirm=True
        )
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["available_blueprints"] == ["other/thing.yaml"]
    assert frames(sent, "blueprint/delete") == []


async def test_delete_success_removes_the_file_and_verifies_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Success re-reads the store: Core's ack carries no result to trust."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    result = await BlueprintTools(core_client).ha_manage_blueprints(
        action="delete", path=PATH, confirm=True
    )

    assert result == {
        "success": True,
        "data": {
            "domain": "automation",
            "path": PATH,
            "message": "Blueprint deleted.",
        },
    }
    assert frames(sent, "blueprint/delete") == [
        {"type": "blueprint/delete", "domain": "automation", "path": PATH}
    ]
    # Pre-check + post-verification.
    assert len(frames(sent, "blueprint/list")) == 2
    assert await installed_text(hass, "automation", PATH) is None


async def test_delete_still_present_after_success_is_an_error(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A blueprint still listed after a 'successful' delete must not report success."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    with patch.object(DomainBlueprints, "async_remove_blueprint", AsyncMock()):
        body = await rejected_call_body(
            BlueprintTools(core_client).ha_manage_blueprints(
                action="delete", path=PATH, confirm=True
            )
        )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "still installed" in body["error"]["message"]


async def test_delete_core_cannot_carry_out_is_service_call_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """A blueprint entry Core lists but cannot unlink: a service fault, not a lock."""
    await set_up_core(hass)
    await hass.async_add_executor_job(
        Path(hass.config.path("blueprints", "automation", "user", "broken.yaml")).mkdir,
        0o777,
        True,
        True,
    )

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="delete", path="user/broken.yaml", confirm=True
        )
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "broken.yaml" in body["error"]["message"]
    assert frames(sent, "search/related") == []


async def test_delete_in_use_names_consumers(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """``Blueprint in use`` becomes RESOURCE_LOCKED carrying the automations that use it."""
    await set_up_core(
        hass,
        {f"automation/{PATH}": MOTION},
        automations=[
            automation_using("Porch light", "binary_sensor.porch"),
            automation_using("Hall light", "binary_sensor.hall"),
        ],
        search=True,
    )

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="delete", path=PATH, confirm=True
        )
    )

    error = body["error"]
    assert error["code"] == "RESOURCE_LOCKED"
    assert body["in_use_by"] == ["automation.hall_light", "automation.porch_light"]
    assert "automation.hall_light" in error["message"]
    assert not any("ha_search" in s for s in error["suggestions"])
    assert await installed_text(hass, "automation", PATH) == MOTION
    # The lookup only enriches an error already being raised, so it carries a
    # short leash rather than send_command's 30s default.
    assert frames(sent, "search/related") == [
        {
            "type": "search/related",
            "item_type": "automation_blueprint",
            "item_id": PATH,
            "_wait_timeout": 5.0,
        }
    ]
    joined = " ".join(error["suggestions"])
    # Removal is what actually frees a blueprint. Taking control converts a
    # consumer but does NOT release it, so the refusal must not send the caller
    # down that path as a fix (#2329).
    assert "ha_config_remove_automation" in joined
    assert "does NOT release the blueprint" in joined
    assert "ha_config_set_automation(entity_id=" in joined
    assert 'action="delete"' in joined


async def test_delete_in_use_script_blueprint_names_script_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """A script blueprint's refusal names script tools and asks about scripts.

    ``ha_config_set_script`` takes ``script_id``; a suggestion built from the
    automation shape cannot be run as written against a script consumer.
    """
    await set_up_core(
        hass,
        {f"script/{SCRIPT_PATH}": BEDTIME},
        scripts={
            "bedtime": {
                "use_blueprint": {
                    "path": SCRIPT_PATH,
                    "input": {"light": "light.hall"},
                }
            }
        },
        search=True,
    )

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="delete", domain="script", path=SCRIPT_PATH, confirm=True
        )
    )

    joined = " ".join(body["error"]["suggestions"])
    assert body["error"]["code"] == "RESOURCE_LOCKED"
    assert body["in_use_by"] == ["script.bedtime"]
    assert "ha_config_remove_script" in joined
    assert "ha_config_set_script(script_id=" in joined
    assert "ha_config_remove_automation" not in joined
    assert "ha_config_set_automation" not in joined
    assert frames(sent, "search/related")[0]["item_type"] == "script_blueprint"


async def test_delete_in_use_when_core_has_no_reference_lookup(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The lock still surfaces when ``search/related`` cannot be consulted."""
    await set_up_core(
        hass,
        {f"automation/{PATH}": MOTION},
        automations=[automation_using("Hall light", "binary_sensor.hall")],
    )

    body = await rejected_call_body(
        BlueprintTools(core_client).ha_manage_blueprints(
            action="delete", path=PATH, confirm=True
        )
    )

    assert body["error"]["code"] == "RESOURCE_LOCKED"
    assert body["in_use_by"] == []
    assert any("ha_search" in s for s in body["error"]["suggestions"])


async def test_delete_in_use_when_the_reference_lookup_drops_the_connection(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A transport failure on the enrichment must not mask the lock."""
    await set_up_core(
        hass,
        {f"automation/{PATH}": MOTION},
        automations=[automation_using("Hall light", "binary_sensor.hall")],
        search=True,
    )
    socket = await pooled(core_client)

    with dropped(hass, "search/related", socket):
        body = await rejected_call_body(
            BlueprintTools(core_client).ha_manage_blueprints(
                action="delete", path=PATH, confirm=True
            )
        )

    assert body["error"]["code"] == "RESOURCE_LOCKED"
    assert body["in_use_by"] == []
    assert any("ha_search" in s for s in body["error"]["suggestions"])


async def test_confirmed_delete_snapshots_the_path_as_given(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only ``save`` normalises: Core's ``blueprint/delete`` takes the store key
    verbatim, and the tool refuses a path that is not one, so the capture keys
    on exactly what the caller passed."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    with patch.object(manager, "maybe_snapshot", wraps=manager.maybe_snapshot) as spy:
        body = await rejected_call_body(
            BlueprintTools(component_core_client).ha_manage_blueprints(
                action="delete", path="user/motion", confirm=True
            )
        )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert [c.args for c in spy.await_args_list] == [
        ("blueprint_automation", "user/motion")
    ]
