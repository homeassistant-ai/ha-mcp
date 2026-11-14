"""Routing tests for the automation tools' ``ha_mcp_tools/automation_config`` use.

With the capability advertised, ``validate`` runs Core's validator before a
write, ``describe`` supplies the registered trigger and condition keys for a
rejection's closest matches, and ``lookup`` resolves the automation through the
registry and carries its entity, validation status and references. Without it,
Core's REST answer leads a rejection, ``validate_config`` names the section and
the ``*_platforms/subscribe`` first event supplies the keys. Either way the
config reaches Core as written and Core's text is never rewritten.

Everything runs on a real ``hass`` through the server's own client; the
component's commands are registered only on the component path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.trigger import async_get_all_descriptions
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools import component_automation
from ha_mcp.tools.automation_errors import REJECTION_PREFIX
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._automation_core import (
    COMPONENT,
    FALLBACK,
    config_reads,
    entity_id_of,
    load_automations_file,
    routed_automation_tools,
    upserts,
)
from ._client_spies import sent_messages
from ._tool_errors import error_body

_PORCH = {"action": "light.turn_on", "target": {"entity_id": "light.porch"}}


@pytest.fixture
async def tools(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The automation tools; on the component path unless parametrized."""
    return await routed_automation_tools(
        getattr(request, "param", COMPONENT), hass, core_client, monkeypatch
    )


_fallback = pytest.mark.parametrize("tools", [FALLBACK], indirect=True)


async def _set(tools: AutomationConfigTools, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await tools.ha_config_set_automation(wait=False, **kwargs)
    return result


async def _rejection(tools: AutomationConfigTools, **kwargs: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await _set(tools, **kwargs)
    body = error_body(exc)
    assert body["error"]["code"] == "VALIDATION_FAILED", body
    return body


async def _created(tools: AutomationConfigTools, alias: str) -> tuple[str, str]:
    """Create an automation through the tool; returns (unique_id, entity_id)."""
    result = await _set(tools, config={"alias": alias, "triggers": [], "actions": []})
    return str(result["unique_id"]), str(result["entity_id"])


class TestComponentValidation:
    async def test_cores_verdict_rejects_before_writing_with_live_closest_keys(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        """A hand-kept rename map missed every key it did not list and went
        stale with each Core release; the hint now comes from what is loaded.
        ``battery.low`` is the 2026.7 rename agents still write."""
        with upserts(core_client) as upsert:
            body = await _rejection(
                tools,
                config={"triggers": [{"trigger": "battery.low"}], "actions": []},
            )

        message = body["error"]["message"]
        assert message.startswith(REJECTION_PREFIX)
        assert "Invalid trigger 'battery.low' specified" in message
        assert body["failed_section"] == "triggers"
        assert body["closest_matches"][0] == "battery.became_low"
        live = await async_get_all_descriptions(hass)
        assert body["platform_description"] == json.loads(
            json.dumps(live["battery.became_low"])
        )
        upsert.assert_not_awaited()

    async def test_a_passing_verdict_writes(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        unique_id, _ = await _created(tools, "Passing")

        stored = await core_client.get_automation_config(unique_id)
        assert stored["alias"] == "Passing"

    async def test_a_component_without_the_command_leaves_it_to_core(
        self,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Refusing here would block every write on an older component. Core
        answers ``unknown_command`` for a command name it never registered."""
        monkeypatch.setattr(
            component_automation,
            "WS_AUTOMATION_CONFIG",
            "ha_mcp_tools/automation_config_retired",
        )
        invalidated = MagicMock(wraps=component_automation.invalidate_caps)
        monkeypatch.setattr(component_automation, "invalidate_caps", invalidated)

        unique_id, _ = await _created(tools, "Older Component")

        assert (await core_client.get_automation_config(unique_id))["alias"] == (
            "Older Component"
        )
        invalidated.assert_called_with(core_client)


@_fallback
class TestCoreRejectionWithoutComponent:
    async def test_cores_text_leads_unchanged(
        self, tools: AutomationConfigTools
    ) -> None:
        """Rewording Core's message hid what Core actually objected to."""
        body = await _rejection(
            tools, config={"trigger": [], "triggers": [], "actions": []}
        )

        message = body["error"]["message"]
        assert message.startswith(f"{REJECTION_PREFIX}Message malformed: ")
        assert "'trigger' and 'triggers'" in message
        assert body["failed_section"] == "schema"

    async def test_failed_section_and_closest_keys_come_from_core(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        assert await async_setup_component(hass, "climate", {})
        socket = await websocket_manager.get_client(
            url=core_client.base_url, token=core_client.token, verify_ssl=False
        )
        with (
            patch.object(
                socket, "subscribe_command", wraps=socket.subscribe_command
            ) as subscribe,
            patch.object(
                socket, "release_subscription", wraps=socket.release_subscription
            ) as release,
        ):
            body = await _rejection(
                tools,
                config={
                    "triggers": [],
                    "conditions": [{"condition": "climate.target_humidity"}],
                    "actions": [],
                },
            )

        assert body["failed_section"] == "conditions"
        assert body["closest_matches"][0] == "climate.is_target_humidity"
        subscribe.assert_awaited_once_with("condition_platforms/subscribe")
        release.assert_awaited_once()

    async def test_a_platform_condition_is_explained_from_cores_text(
        self, tools: AutomationConfigTools
    ) -> None:
        """The precheck for this misuse is gone; Core's rejection carries the hint."""
        body = await _rejection(
            tools,
            config={
                "triggers": [],
                "conditions": [{"platform": "state"}],
                "actions": [],
            },
        )

        assert "'condition', e.g." in body["error"]["suggestion"]


@_fallback
async def test_the_config_is_sent_as_given(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """Dropping the singular form wrote a different automation than the
    caller sent; Core is the one that refuses both forms together."""
    config: dict[str, Any] = {"trigger": [], "triggers": [], "actions": []}

    with upserts(core_client) as upsert, pytest.raises(ToolError):
        await _set(tools, config=dict(config))

    assert upsert.call_args.args[0] == config


@_fallback
class TestOperationFromExistence:
    async def test_a_fresh_explicit_id_is_reported_created(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """#2903: an explicit new id was reported as an update."""
        with upserts(core_client) as upsert:
            result = await _set(
                tools,
                entity_id="fresh_id",
                config={"alias": "Fresh", "triggers": [], "actions": []},
            )

        assert result["operation"] == "created"
        assert upsert.call_args.kwargs["wait_for_entity"]

    async def test_an_existing_id_is_reported_updated(
        self, tools: AutomationConfigTools
    ) -> None:
        unique_id, _ = await _created(tools, "Lights")

        result = await _set(
            tools,
            entity_id=unique_id,
            config={"alias": "Lights", "triggers": [], "actions": [_PORCH]},
        )

        assert result["operation"] == "updated"


async def test_an_entity_id_the_registry_lacks_is_not_found_never_created(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    with upserts(core_client) as upsert, pytest.raises(ToolError) as exc:
        await _set(tools, entity_id="automation.ghost", config={})

    assert error_body(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"
    upsert.assert_not_awaited()


class TestComponentLookup:
    async def test_get_carries_the_components_registry_details(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        result = await _set(
            tools, config={"alias": "Lights", "triggers": [], "actions": [_PORCH]}
        )
        unique_id, entity_id = str(result["unique_id"]), str(result["entity_id"])
        label = lr.async_get(hass).async_create("Evening")
        category = cr.async_get(hass).async_create(scope="automation", name="Lighting")
        er.async_get(hass).async_update_entity(
            entity_id,
            labels={label.label_id},
            categories={"automation": category.category_id},
        )
        row = await core_client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )

        with config_reads(core_client) as reads, sent_messages(core_client) as sent:
            got = await tools.ha_config_get_automation(entity_id=entity_id)

        assert got["entity"] == row["result"]
        assert got["validation"] is None
        assert got["references"]["entities"] == ["light.porch"]
        assert got["references"]["blueprint"] is None
        assert "related" not in got
        assert got["config"]["category"] == category.category_id
        assert "warnings" not in got
        # The body still comes from Core's REST read, never the component.
        reads.assert_awaited_once_with(unique_id)
        assert {c.args[0]["type"] for c in sent.call_args_list} == {
            "ha_mcp_tools/automation_config"
        }

    async def test_an_unloaded_automation_says_why_it_has_no_references(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        """A disabled automation came back with references None and no reason,
        which read as an automation that references nothing."""
        _, entity_id = await _created(tools, "Disabled")
        er.async_get(hass).async_update_entity(
            entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()

        got = await tools.ha_config_get_automation(entity_id=entity_id)

        assert got["references"] is None
        assert len(got["warnings"]) == 1
        assert "has not loaded this automation" in got["warnings"][0]

    async def test_a_failed_automation_carries_its_failure_and_no_references(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        """Core holds only an unavailable placeholder for an automation that
        failed validation, so the bridge reports it as not loaded: the read
        carries Core's verdict, no references and the reason."""
        await load_automations_file(
            hass,
            "automations.yaml",
            [
                {
                    "id": "1700000000000",
                    "alias": "Broken",
                    "triggers": [],
                    "actions": [{"delay": "not a duration"}],
                }
            ],
        )

        entity_id = entity_id_of(hass, "1700000000000")

        got = await tools.ha_config_get_automation(entity_id=entity_id)

        # Core files the failed delay as a schema failure.
        issue = ir.async_get(hass).async_get_issue(
            "automation", f"{entity_id}_validation_failed_schema"
        )
        assert issue is not None and issue.translation_placeholders is not None
        assert got["validation"] == {
            "status": "failed_schema",
            "error": issue.translation_placeholders["error"],
        }
        assert got["references"] is None
        assert len(got["warnings"]) == 1
        assert "failed to load" in got["warnings"][0]

    async def test_a_disabled_automation_is_removed_by_entity_id(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        """A disabled automation has no state, so a state lookup reported it
        missing and it could not be removed."""
        unique_id, entity_id = await _created(tools, "Disabled")
        er.async_get(hass).async_update_entity(
            entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id) is None

        result = await tools.ha_config_remove_automation(
            entity_id=entity_id, wait=False
        )

        assert result["automation_id"] == entity_id
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_get_automation(entity_id=unique_id)
        assert error_body(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"
