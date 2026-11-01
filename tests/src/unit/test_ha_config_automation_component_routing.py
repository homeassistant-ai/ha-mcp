"""Routing tests for the automation tools' ``ha_mcp_tools/automation_config`` use.

With the capability advertised, ``validate`` runs Core's validator before a
write, ``describe`` supplies the registered trigger and condition keys for a
rejection's closest matches, and ``lookup`` resolves the automation through the
registry and carries its entity, validation status and references. Without it,
Core's REST answer leads a rejection, ``validate_config`` names the section and
the ``*_platforms/subscribe`` first event supplies the keys. Either way the
config reaches Core as written and Core's text is never rewritten.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import automation_errors, component_api, component_automation
from ha_mcp.tools.automation_errors import REJECTION_PREFIX
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_call_service_component_routing import (
    _clear_caps_cache,  # noqa: F401 - autouse fixture shared across routing tests
)

_CAPS = {
    "schema_version": 1,
    "component_version": "9.9.9",
    "capabilities": ["automation_config"],
    "limits": {},
}
_NO_CAPS = {**_CAPS, "capabilities": ["search"]}
_STORED = {"id": "1700000000000", "alias": "Lights", "triggers": [], "actions": []}


def _core_400(text: str) -> HomeAssistantAPIError:
    return HomeAssistantAPIError(
        f"API error: 400 - {text}", status_code=400, response_data={"message": text}
    )


class Client:
    """Credentialed spy answering the component by action and Core by mock."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.replies = replies or {}
        self.validate_config: dict[str, Any] = {}
        self.sent: list[dict[str, Any]] = []
        self.get_automation_config = AsyncMock(return_value=dict(_STORED))
        self._resolve_automation_id = AsyncMock(return_value="1700000000000")
        self.get_states = AsyncMock(return_value=[])
        self.get_services = AsyncMock(return_value=[])
        self.upsert_automation_config = AsyncMock(
            return_value={"unique_id": "1700000000000", "entity_id": None}
        )
        self.delete_automation_config = AsyncMock(
            return_value={"unique_id": "1700000000000", "result": "ok"}
        )

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(msg)
        if msg["type"] == "ha_mcp_tools/automation_config":
            reply = self.replies[msg["action"]]
            if "error_code" in reply:
                return {"success": False, **reply}
            return {"success": True, "result": reply}
        if msg["type"] == "validate_config":
            return {"success": True, "result": self.validate_config}
        return {"success": True, "result": []}


async def _set(client: Client, caps: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    with patch_ws(make_ws("unused", info_result=caps), component_automation):
        return await AutomationConfigTools(client).ha_config_set_automation(
            wait=False, **kwargs
        )


async def _rejection(client: Client, caps: dict[str, Any], **kwargs: Any) -> dict:
    with pytest.raises(ToolError) as exc:
        await _set(client, caps, **kwargs)
    body: dict[str, Any] = json.loads(str(exc.value))
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return body


_BATTERY = {
    "fields": {"behavior": {}},
    "target": {"entity": {"domain": "binary_sensor"}},
}


class TestComponentValidation:
    @pytest.mark.parametrize(
        ("invalid", "live", "expected"),
        [
            # The 2026.7 rename agents still write.
            (
                "battery.low",
                {"battery.became_low": _BATTERY, "state": None},
                "battery.became_low",
            ),
            # No list kept in HA-MCP could know a third-party key.
            (
                "acme.gizmo_tripped",
                {"acme.gizmo_triggered": _BATTERY},
                "acme.gizmo_triggered",
            ),
        ],
        ids=["renamed-core-key", "third-party-key"],
    )
    async def test_cores_verdict_rejects_before_writing_with_live_closest_keys(
        self, invalid: str, live: dict[str, Any], expected: str
    ) -> None:
        """A hand-kept rename map missed every key it did not list and went
        stale with each Core release; the hint now comes from what is loaded."""
        error = f"Invalid trigger '{invalid}' specified"
        client = Client(
            {
                "validate": {"status": "failed_triggers", "error": error},
                "describe": {"descriptions": live},
            }
        )

        body = await _rejection(
            client, _CAPS, config={"triggers": [{"trigger": invalid}], "actions": []}
        )

        assert body["error"]["message"] == f"{REJECTION_PREFIX}{error}"
        assert body["failed_section"] == "triggers"
        assert body["closest_matches"][0] == expected
        assert body["platform_description"] == live[expected]
        client.upsert_automation_config.assert_not_awaited()

    async def test_a_passing_verdict_writes(self) -> None:
        client = Client({"validate": {"status": "ok", "error": None}})

        await _set(client, _CAPS, config={"triggers": [], "actions": []})

        client.upsert_automation_config.assert_awaited_once()

    @pytest.mark.parametrize(
        "reply",
        [
            {"status": "unavailable", "error": "no validator in this Core"},
            {"error_code": "unknown_command", "error": "Unknown command."},
        ],
        ids=["unavailable", "unknown-command"],
    )
    async def test_a_component_that_cannot_validate_leaves_it_to_core(
        self, reply: dict[str, Any]
    ) -> None:
        """Refusing here would block every write on an older component."""
        client = Client({"validate": reply})

        await _set(client, _CAPS, config={"triggers": [], "actions": []})

        client.upsert_automation_config.assert_awaited_once()
        if "error_code" in reply:
            assert client not in component_api._CAPS_CACHE


class TestCoreRejectionWithoutComponent:
    async def test_cores_text_leads_unchanged(self) -> None:
        """Rewording Core's message hid what Core actually objected to."""
        text = (
            "Message malformed: Cannot specify both 'trigger' and 'triggers'. "
            "Please use 'triggers' only."
        )
        client = Client()
        client.upsert_automation_config.side_effect = _core_400(text)
        client.validate_config = {"triggers": {"valid": True, "error": None}}

        body = await _rejection(
            client, _NO_CAPS, config={"trigger": [], "triggers": [], "actions": []}
        )

        assert body["error"]["message"] == f"{REJECTION_PREFIX}{text}"
        assert body["failed_section"] == "schema"

    async def test_failed_section_and_closest_keys_come_from_core(self) -> None:
        text = (
            "Message malformed: Invalid condition 'climate.target_humidity' specified"
        )
        client = Client()
        client.upsert_automation_config.side_effect = _core_400(text)
        client.validate_config = {
            "triggers": {"valid": True, "error": None},
            "conditions": {"valid": False, "error": text},
        }
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        queue.put_nowait(
            {"id": 7, "type": "event", "event": {"climate.is_target_humidity": {}}}
        )
        ws = AsyncMock()
        ws.subscribe_command = AsyncMock(return_value=(7, queue))

        with patch.object(
            automation_errors, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            body = await _rejection(
                client,
                _NO_CAPS,
                config={
                    "triggers": [],
                    "conditions": [{"condition": "climate.target_humidity"}],
                    "actions": [],
                },
            )

        assert body["failed_section"] == "conditions"
        assert body["closest_matches"] == ["climate.is_target_humidity"]
        ws.subscribe_command.assert_awaited_once_with("condition_platforms/subscribe")
        ws.release_subscription.assert_awaited_once_with(7)

    async def test_a_platform_condition_is_explained_from_cores_text(self) -> None:
        """The precheck for this misuse is gone; Core's rejection carries the hint."""
        text = (
            "Message malformed: Unexpected value for condition: 'None'. Expected a "
            "condition, a list of conditions or a valid template"
        )
        client = Client()
        client.upsert_automation_config.side_effect = _core_400(text)

        body = await _rejection(
            client,
            _NO_CAPS,
            config={
                "triggers": [],
                "conditions": [{"platform": "state"}],
                "actions": [],
            },
        )

        assert "'condition', e.g." in body["error"]["suggestion"]


class TestConfigReachesCoreAsWritten:
    @pytest.mark.parametrize(
        "config",
        [
            {"trigger": [], "triggers": [], "actions": []},
            {"triggers": [{"trigger": "time", "at": "07:00:00"}], "actions": []},
        ],
        ids=["both-root-forms", "no-alias"],
    )
    async def test_the_config_is_sent_as_given(self, config: dict[str, Any]) -> None:
        """Dropping the singular form wrote a different automation than the
        caller sent, and the alias precheck refused one Core accepts."""
        client = Client()

        await _set(client, _NO_CAPS, config=dict(config))

        assert client.upsert_automation_config.call_args.args[0] == config


class TestOperationFromExistence:
    async def test_a_fresh_explicit_id_is_reported_created(self) -> None:
        """#2903: an explicit new id was reported as an update."""
        client = Client()
        client.get_automation_config.side_effect = [
            HomeAssistantAPIError("missing", status_code=404),
            dict(_STORED),
        ]

        result = await _set(
            client,
            _NO_CAPS,
            entity_id="fresh_id",
            config={"triggers": [], "actions": []},
        )

        assert result["operation"] == "created"
        assert client.upsert_automation_config.call_args.kwargs["wait_for_entity"]

    async def test_an_existing_id_is_reported_updated(self) -> None:
        client = Client()

        result = await _set(
            client, _NO_CAPS, entity_id="1700000000000", config={"alias": "Lights"}
        )

        assert result["operation"] == "updated"

    async def test_an_entity_id_the_registry_lacks_is_not_found_never_created(
        self,
    ) -> None:
        client = Client({"lookup": {"found": False, "entity": None}})

        with pytest.raises(ToolError) as exc:
            await _set(client, _CAPS, entity_id="automation.ghost", config={})

        assert json.loads(str(exc.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
        client.upsert_automation_config.assert_not_awaited()


_LOOKUP = {
    "found": True,
    "entity": {
        "entity_id": "automation.lights",
        "unique_id": "1700000000000",
        "labels": ["evening"],
        "categories": {"automation": "lighting"},
        "disabled_by": "user",
    },
    "loaded": True,
    "validation": None,
    "references": {"entities": ["light.porch"], "blueprint": None},
}


class TestComponentLookup:
    async def test_get_carries_the_components_registry_details(self) -> None:
        client = Client({"lookup": _LOOKUP})

        with patch_ws(make_ws("unused", info_result=_CAPS), component_automation):
            result = await AutomationConfigTools(client).ha_config_get_automation(
                entity_id="automation.lights"
            )

        assert result["entity"] == _LOOKUP["entity"]
        assert result["validation"] == _LOOKUP["validation"]
        assert result["references"] == _LOOKUP["references"]
        assert result["config"]["category"] == "lighting"
        assert "warnings" not in result
        # The body still comes from Core's REST read, never the component.
        client.get_automation_config.assert_awaited_once_with("1700000000000")
        assert client.types() == ["ha_mcp_tools/automation_config"]

    async def test_an_unloaded_automation_says_why_it_has_no_references(
        self,
    ) -> None:
        """A disabled automation came back with references None and no reason,
        which read as an automation that references nothing."""
        client = Client({"lookup": {**_LOOKUP, "loaded": False, "references": None}})

        with patch_ws(make_ws("unused", info_result=_CAPS), component_automation):
            result = await AutomationConfigTools(client).ha_config_get_automation(
                entity_id="automation.lights"
            )

        assert result["references"] is None
        assert len(result["warnings"]) == 1
        assert "has not loaded this automation" in result["warnings"][0]

    async def test_a_failed_automation_carries_its_failure_and_no_references(
        self,
    ) -> None:
        """Core holds only an unavailable placeholder for an automation that
        failed validation, so the bridge reports it as not loaded: the read
        carries Core's verdict, no references and the reason."""
        client = Client(
            {
                "lookup": {
                    **_LOOKUP,
                    "loaded": False,
                    "validation": {
                        "status": "failed_actions",
                        "error": "Unknown action",
                    },
                    "references": None,
                }
            }
        )

        with patch_ws(make_ws("unused", info_result=_CAPS), component_automation):
            result = await AutomationConfigTools(client).ha_config_get_automation(
                entity_id="automation.lights"
            )

        assert result["validation"] == {
            "status": "failed_actions",
            "error": "Unknown action",
        }
        assert result["references"] is None
        assert len(result["warnings"]) == 1
        assert "failed to load" in result["warnings"][0]

    async def test_a_disabled_automation_is_removed_by_entity_id(self) -> None:
        """A disabled automation has no state, so a state lookup reported it
        missing and it could not be removed."""
        client = Client({"lookup": _LOOKUP})

        with patch_ws(make_ws("unused", info_result=_CAPS), component_automation):
            result = await AutomationConfigTools(client).ha_config_remove_automation(
                entity_id="automation.lights", wait=False
            )

        assert result["automation_id"] == "automation.lights"
        client.delete_automation_config.assert_awaited_once_with("1700000000000")
        client._resolve_automation_id.assert_not_awaited()
