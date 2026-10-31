"""Which automation a read, write or removal reaches, and what the tools say
when Core's config API cannot reach it.

Core's config API reads and edits only ``automations.yaml``. An automation
from YAML or a package still has a registry row (its ``id`` is the unique_id),
so the tools can tell it apart from one that does not exist.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import component_automation, component_config_reads
from ha_mcp.tools.reference_validator import validate_config_references
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._component_routing_helpers import make_ws, patch_ws
from .test_component_automation_config import (
    _run,
    core,  # noqa: F401 - pytest fixture
)
from .test_component_ws_search import _StubHomeAssistantError
from .test_ha_call_service_component_routing import (
    _clear_caps_cache,  # noqa: F401 - autouse fixture shared across routing tests
)
from .test_ha_config_automation_component_routing import _CAPS, _NO_CAPS, Client

_ID = "1700000000000"
_ENTITY = "automation.lights"
_FOUND = {
    "found": True,
    "entity": {"entity_id": _ENTITY, "unique_id": _ID, "platform": "automation"},
    "validation": None,
    "references": None,
}
_NO_ROW = {"found": False, "entity": None, "validation": None, "references": None}
_CONFIG = {"alias": "Lights", "triggers": [], "actions": []}


def _not_stored() -> HomeAssistantAPIError:
    return HomeAssistantAPIError(f"Automation not found: {_ID}", status_code=404)


async def _refusal(
    client: Client, caps: dict[str, Any], tool: str, **kwargs: Any
) -> dict[str, Any]:
    with (
        patch_ws(make_ws("unused", info_result=caps), component_automation),
        pytest.raises(ToolError) as exc,
    ):
        await getattr(AutomationConfigTools(client), tool)(**kwargs)
    body: dict[str, Any] = json.loads(str(exc.value))
    return body


# --- an automation Core's config API cannot reach --------------------------------


class TestAutomationOutsideAutomationsYaml:
    @pytest.mark.parametrize(
        ("caps", "replies", "identifier"),
        [
            (_CAPS, {"lookup": _FOUND}, _ENTITY),
            (_NO_CAPS, {}, _ENTITY),
            (_CAPS, {"lookup": _FOUND}, _ID),
        ],
        ids=["component-entity-id", "fallback-entity-id", "component-config-id"],
    )
    async def test_get_says_the_automation_is_not_editable_here(
        self, caps: dict[str, Any], replies: dict[str, Any], identifier: str
    ) -> None:
        """A package automation was reported as not existing although Home
        Assistant runs it, and its id was missing from the ids offered."""
        client = Client(replies)
        client.get_automation_config.side_effect = _not_stored()

        body = await _refusal(
            client, caps, "ha_config_get_automation", entity_id=identifier
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["config_id"] == _ID, body

    async def test_write_to_it_is_refused_not_created(self) -> None:
        """Writing would create a second automation in automations.yaml under
        the same id instead of editing the one that runs."""
        client = Client({"lookup": _FOUND})
        client.get_automation_config.side_effect = _not_stored()

        body = await _refusal(
            client,
            _CAPS,
            "ha_config_set_automation",
            entity_id=_ENTITY,
            config=dict(_CONFIG),
            wait=False,
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        client.upsert_automation_config.assert_not_awaited()

    async def test_remove_says_the_automation_is_not_editable_here(self) -> None:
        client = Client({"lookup": _FOUND})
        client.get_automation_config.side_effect = _not_stored()
        client.delete_automation_config.side_effect = _not_stored()

        body = await _refusal(
            client,
            _CAPS,
            "ha_config_remove_automation",
            entity_id=_ENTITY,
            wait=False,
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["config_id"] == _ID, body

    async def test_config_id_without_a_registry_row_is_not_found(self) -> None:
        client = Client({"lookup": _NO_ROW})
        client.get_automation_config.side_effect = _not_stored()

        body = await _refusal(client, _CAPS, "ha_config_get_automation", entity_id=_ID)

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND", body


async def test_hash_for_an_unstored_config_id_is_refused() -> None:
    """A config_hash comes from reading a stored automation; writing anyway
    would create a new one where the caller meant to edit."""
    client = Client()
    client.get_automation_config.side_effect = _not_stored()

    body = await _refusal(
        client,
        _NO_CAPS,
        "ha_config_set_automation",
        entity_id="fresh_id",
        config_hash="0123456789abcdef",
        config=dict(_CONFIG),
        wait=False,
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND", body
    client.upsert_automation_config.assert_not_awaited()


# --- the load failure without the component --------------------------------------


class _RepairsClient(Client):
    """Answers ``repairs/list_issues`` with the given automation issues."""

    def __init__(self, issue_ids: list[str]) -> None:
        super().__init__()
        self.issues = [
            {
                "domain": "automation",
                "issue_id": issue_id,
                "translation_placeholders": {"error": "Missing input event"},
            }
            for issue_id in issue_ids
        ]

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg["type"] == "repairs/list_issues":
            return {"success": True, "result": {"issues": self.issues}}
        return await super().send_websocket_message(msg)


@pytest.mark.parametrize(
    ("issue_id", "validation"),
    [
        (
            "automation.zz_pv_validation_failed_blueprint",
            {"status": "failed_blueprint", "error": "Missing input event"},
        ),
        # automation.zz_pv_validation_failed_blueprint's own issue.
        (
            "automation.zz_pv_validation_failed_blueprint_validation_failed_blueprint",
            None,
        ),
    ],
    ids=["own-issue", "issue-of-an-entity-id-extending-it"],
)
async def test_load_failure_is_read_from_the_automations_own_issue(
    issue_id: str, validation: dict[str, Any] | None
) -> None:
    """A healthy automation was reported broken, with a doubled status, when
    another automation's entity_id started with its own."""
    client = _RepairsClient([issue_id])

    with patch_ws(make_ws("unused", info_result=_NO_CAPS), component_automation):
        got = await AutomationConfigTools(client).ha_config_get_automation(
            entity_id="automation.zz_pv"
        )

    assert got["validation"] == validation


# --- target references ------------------------------------------------------------

_REF_CAPS = {**_CAPS, "capabilities": ["reference_data", "reference_data_targets"]}
_LIGHT_SERVICES = [{"domain": "light", "services": {"turn_on": {}}}]


async def _references(
    reply: dict[str, Any] | None, config: dict[str, Any]
) -> dict[str, Any]:
    ws = make_ws(
        "ha_mcp_tools/reference_data",
        info_result=_NO_CAPS if reply is None else _REF_CAPS,
        cmd_result=reply,
    )
    with patch_ws(ws, component_config_reads):
        return await validate_config_references(Client(), config)


@pytest.mark.parametrize(
    ("reply", "unchecked"),
    [
        (None, ["area", "device", "label"]),
        (
            {
                "services": _LIGHT_SERVICES,
                "entity_ids": [],
                "device_ids": ["d1"],
                "area_ids": ["kitchen"],
            },
            ["label"],
        ),
    ],
    ids=["without-component", "component-without-label-ids"],
)
async def test_targets_left_unchecked_are_named(
    reply: dict[str, Any] | None, unchecked: list[str]
) -> None:
    """An empty warning list read as "every target exists" while device,
    area, floor and label ids were never compared with a registry."""
    config = {
        "actions": [
            {
                "action": "light.turn_on",
                "target": {
                    "device_id": "d1",
                    "area_id": ["kitchen", "hall"],
                    "label_id": "night",
                },
            }
        ]
    }

    result = await _references(reply, config)

    assert result["unchecked_target_kinds"] == unchecked


@pytest.mark.parametrize(
    ("sentinels", "warned"),
    [(["none", "all"], set()), (None, {"none", "all"})],
    ids=["core-sent-them", "older-component"],
)
async def test_cores_match_sentinels_are_not_unknown_targets(
    sentinels: list[str] | None, warned: set[str]
) -> None:
    """Core accepts ``none`` and ``all`` as targets; they were warned as
    missing registry items. The values come from Core through the component,
    so a reply without them changes nothing."""
    reply: dict[str, Any] = {
        "services": _LIGHT_SERVICES,
        "entity_ids": [],
        "device_ids": [],
        "area_ids": [],
        "floor_ids": [],
        "label_ids": [],
    }
    if sentinels is not None:
        reply["match_sentinels"] = sentinels
    config = {
        "actions": [
            {
                "action": "light.turn_on",
                "target": {"entity_id": "none", "area_id": "all"},
            }
        ]
    }

    result = await _references(reply, config)

    assert {w["value"] for w in result["warnings"]} == warned


# --- component refusals -------------------------------------------------------------


@pytest.mark.parametrize("action", ["validate", "describe"])
async def test_request_without_its_input_is_refused_not_answered(
    core: Any,  # noqa: F811
    action: str,
) -> None:
    """Answered, a validate without a config or a describe without a kind
    would read as Core's verdict on nothing."""
    with pytest.raises(_StubHomeAssistantError):
        await _run(core, action)
