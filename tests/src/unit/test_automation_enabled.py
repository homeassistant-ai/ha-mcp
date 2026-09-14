"""Focused tests for automation enable/disable service routing."""

from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import tools_config_automations


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.upserted: list[dict[str, Any]] = []
        self.upsert_entity_id: str | None = None
        self.states: list[dict[str, Any]] = []
        self.states_error: Exception | None = None
        self.service_error: Exception | None = None

    async def upsert_automation_config(
        self,
        config: dict[str, Any],
        identifier: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.upserted.append(config)
        return {"success": True, "entity_id": self.upsert_entity_id}

    async def get_states(self) -> list[dict[str, Any]]:
        if self.states_error:
            raise self.states_error
        return self.states

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        if self.service_error:
            raise self.service_error
        self.calls.append((domain, service, data))
        return {"success": True}


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_applies_enabled_without_writing_config_key() -> None:
    client = _FakeClient()
    client.upsert_entity_id = "automation.morning"
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "automation.morning",
        None,
        False,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=True,
    )

    assert result["enabled"] is True
    assert "enabled" not in client.upserted[0]
    assert client.calls == [
        ("automation", "turn_on", {"entity_id": "automation.morning"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_resolves_unique_id_before_enabling() -> None:
    client = _FakeClient()
    client.states = [
        {
            "entity_id": "automation.actual",
            "attributes": {"id": "stored-id"},
        }
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "stored-id",
        None,
        False,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=False,
    )

    assert result["enabled"] is False
    assert client.calls == [
        ("automation", "turn_off", {"entity_id": "automation.actual"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_set_automation_enabled_routes_false_to_turn_off() -> None:
    client = _FakeClient()

    result = await tools_config_automations._set_automation_enabled(
        client, "automation.morning", False
    )

    assert result == {"success": True}
    assert client.calls == [
        ("automation", "turn_off", {"entity_id": "automation.morning"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_reports_resolved_entity_id_when_upsert_omits_it() -> None:
    client = _FakeClient()
    client.states = [
        {
            "entity_id": "automation.actual",
            "attributes": {"id": "stored-id"},
        }
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "stored-id",
        None,
        False,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=False,
    )

    assert result["automation_id"] == "automation.actual"


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_rejects_enabled_in_stored_config() -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            config={
                "alias": "Morning",
                "triggers": [],
                "actions": [],
                "enabled": False,
            },
            wait=False,
        )

    assert "enabled" in str(exc_info.value).lower()
    assert client.upserted == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_reports_runtime_state_failure_as_partial_success() -> None:
    client = _FakeClient()
    client.upsert_entity_id = "automation.morning"
    client.service_error = RuntimeError("service unavailable")
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "automation.morning",
        None,
        False,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=False,
    )

    assert result["success"] is True
    assert result["enabled_applied"] is False
    assert "config was written" in result["warnings"][0]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_preserves_connection_errors() -> None:
    client = _FakeClient()
    client.states_error = HomeAssistantConnectionError("connection lost")
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(HomeAssistantConnectionError):
        await tools._set_enabled_only("stored-id", False, wait=False)


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_rejects_category_without_config_update() -> None:
    client = _FakeClient()
    client.states = [
        {
            "entity_id": "automation.morning",
            "attributes": {"id": "stored-id"},
        }
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            identifier="stored-id",
            enabled=True,
            category="morning",
            wait=False,
        )

    assert "category" in str(exc_info.value).lower()
    assert client.calls == []
