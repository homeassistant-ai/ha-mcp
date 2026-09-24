"""Focused tests for automation enable/disable service routing."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import auto_backup, tools_config_automations


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.upserted: list[dict[str, Any]] = []
        self.upsert_entity_id: str | None = None
        self.upsert_unique_id: str | None = None
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
        result: dict[str, Any] = {
            "success": True,
            "entity_id": self.upsert_entity_id,
        }
        if self.upsert_unique_id is not None:
            result["unique_id"] = self.upsert_unique_id
        return result

    async def get_states(self) -> list[dict[str, Any]]:
        if self.states_error:
            raise self.states_error
        return self.states

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        service_error = self.service_error
        if service_error is not None:
            raise service_error
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
async def test_config_update_waits_for_reregistration_when_wait_false(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    discovered: list[str] = []

    async def wait_for_unique_id(client, identifier):
        discovered.append(identifier)
        return "automation.actual"

    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        wait_for_unique_id,
    )

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

    assert discovered == ["stored-id"]
    assert result["enabled_applied"] is True
    assert client.calls == [
        ("automation", "turn_off", {"entity_id": "automation.actual"})
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
async def test_standalone_enabled_attaches_skill_content(monkeypatch) -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    def attach_skill_content(
        response, *, MandatoryBPS, canonical_files, referenced_files
    ):
        if MandatoryBPS:
            response["skill_content"] = {"automation": "guidance"}

    monkeypatch.setattr(
        tools_config_automations, "attach_skill_content", attach_skill_content
    )

    result = await tools.ha_config_set_automation(
        identifier="automation.morning",
        enabled=True,
        MandatoryBPS=True,
        wait=False,
    )

    assert result["success"] is True
    assert result["action"] == "set_enabled"
    assert result["skill_content"] == {"automation": "guidance"}


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_service_failure_raises_tool_error() -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    client.service_error = RuntimeError("service unavailable")
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            identifier="automation.morning",
            enabled=True,
            MandatoryBPS=False,
            wait=False,
        )

    error = json.loads(str(exc_info.value))
    assert error["success"] is False
    assert error["error"]["code"] == "INTERNAL_ERROR"
    assert error["error"]["details"] == "service unavailable"


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
@pytest.mark.parametrize(
    ("verification_result", "expected_warning"),
    [({"state": "on"}, None), (None, "could not be verified")],
)
async def test_standalone_enabled_verifies_requested_state(
    monkeypatch, verification_result, expected_warning
) -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)
    verification_calls: list[tuple[str, str]] = []

    async def wait_for_state(client, entity_id, *, expected_state):
        verification_calls.append((entity_id, expected_state))
        return verification_result

    monkeypatch.setattr(
        tools_config_automations, "wait_for_state_change", wait_for_state
    )

    result = await tools.ha_config_set_automation(
        identifier="automation.morning",
        enabled=True,
        MandatoryBPS=False,
        wait=True,
    )

    assert result["enabled_applied"] is True
    assert verification_calls == [("automation.morning", "on")]
    if expected_warning is not None:
        assert any(expected_warning in warning for warning in result["warnings"])


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_prefixed_identifier_must_exist() -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools._set_enabled_only("automation.missing", False, wait=False)

    assert "not found" in str(exc_info.value).lower()
    assert client.calls == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_raw_unique_id_creation_waits_for_registration_before_enabling(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    unique_id = "new-automation-id"
    discovered_entity_id = "automation.generated_name"
    discovered: list[str] = []

    async def wait_for_unique_id(client, identifier):
        discovered.append(identifier)
        return discovered_entity_id

    async def wait_for_entity(*_args, **_kwargs):
        return True

    async def wait_for_state(*_args, **_kwargs):
        return {"state": "on"}

    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        wait_for_unique_id,
        raising=False,
    )
    monkeypatch.setattr(
        tools_config_automations, "wait_for_entity_registered", wait_for_entity
    )
    monkeypatch.setattr(
        tools_config_automations, "wait_for_state_change", wait_for_state
    )

    result = await tools._run_config_update(
        {"alias": "Generated", "triggers": [], "actions": []},
        unique_id,
        None,
        True,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=True,
    )

    assert discovered == [unique_id]
    assert result["automation_id"] == discovered_entity_id
    assert result["enabled_applied"] is True
    assert client.calls == [
        ("automation", "turn_on", {"entity_id": discovered_entity_id})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_generated_creation_resolves_unique_id_before_enabling(
    monkeypatch,
) -> None:
    client = _FakeClient()
    client.upsert_unique_id = "generated-unique-id"
    tools = tools_config_automations.AutomationConfigTools(client)
    registration_calls: list[str] = []

    async def wait_for_unique_id(client, identifier):
        registration_calls.append(identifier)
        return "automation.generated"

    async def wait_for_entity(*_args, **_kwargs):
        return True

    async def wait_for_state(*_args, **_kwargs):
        return {"state": "on"}

    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        wait_for_unique_id,
    )
    monkeypatch.setattr(
        tools_config_automations, "wait_for_entity_registered", wait_for_entity
    )
    monkeypatch.setattr(
        tools_config_automations, "wait_for_state_change", wait_for_state
    )

    result = await tools._run_config_update(
        {"alias": "Generated", "triggers": [], "actions": []},
        None,
        None,
        True,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=True,
    )

    assert registration_calls == ["generated-unique-id"]
    assert result["automation_id"] == "automation.generated"
    assert result["enabled_applied"] is True
    assert client.calls == [
        ("automation", "turn_on", {"entity_id": "automation.generated"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_runtime_toggle_skips_auto_backup(monkeypatch) -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)
    snapshot_calls: list[tuple[str, str]] = []

    class _BackupManager:
        async def maybe_snapshot(self, domain, entity_id, **_kwargs):
            snapshot_calls.append((domain, entity_id))

    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True),
    )
    monkeypatch.setattr(
        auto_backup, "get_backup_manager", lambda *_args: _BackupManager()
    )

    result = await tools.ha_config_set_automation(
        identifier="automation.morning", enabled=True, wait=False
    )

    assert result["enabled_applied"] is True
    assert snapshot_calls == []


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
