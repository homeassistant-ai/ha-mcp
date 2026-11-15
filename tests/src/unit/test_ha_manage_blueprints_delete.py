"""``ha_manage_blueprints`` parameter checks and ``delete`` on real Core (#2329).

Blueprints are files Core loads from the test's config directory; automations
and scripts using them are set up from YAML, so Core's own usage index decides
whether a delete is refused and which consumers ``search/related`` names:

- the ``confirm=True`` gate and its existence pre-check,
- ``BlueprintInUse`` becoming ``RESOURCE_LOCKED`` with the consumers Core names,
  and the degraded variants when that lookup cannot be made,
- the post-delete state verification Core's empty ack cannot provide,
- the auto-backup capture that precedes a delete.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.blueprint.models import DomainBlueprints
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_blueprints import BlueprintTools

from ._blueprint_actions_core import (
    BEDTIME,
    MOTION,
    PATH,
    SCRIPT_PATH,
    automation_using,
    enable_auto_backup,
    frames,
    installed_text,
    set_up_core,
    snapshot_texts,
)
from ._component_frames import dropped, pooled
from ._tool_errors import rejected_call_body


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


async def test_unconfirmed_delete_captures_no_backup(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With auto-backup ON, a refused delete captures nothing.

    The refusal changes nothing, so a snapshot for it is pure cost, paid
    before the tool says no.
    """
    await set_up_core(hass, {f"automation/{PATH}": MOTION})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    body = await rejected_call_body(
        BlueprintTools(component_core_client).ha_manage_blueprints(
            action="delete", path=PATH
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert manager.list_snapshots() == []


async def test_confirmed_delete_snapshots_the_blueprint_first(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The snapshot is the only way back from a deleted blueprint file."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    await BlueprintTools(component_core_client).ha_manage_blueprints(
        action="delete", path=PATH, confirm=True
    )

    assert snapshot_texts(manager, "blueprint_automation", PATH) == [MOTION]
    assert await installed_text(hass, "automation", PATH) is None


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
