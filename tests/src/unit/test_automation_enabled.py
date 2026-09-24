"""Focused tests for automation enable/disable service routing."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import auto_backup, tools_config_automations, util_helpers


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
    assert result["entity_id"] == "automation.actual"
    assert client.calls == [
        ("automation", "turn_off", {"entity_id": "automation.actual"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_python_transform_reports_entity_resolved_for_enabled(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    config = {
        "alias": "Morning",
        "triggers": [{"trigger": "event", "event_type": "test_event"}],
        "actions": [{"action": "logbook.log", "data": {"message": "x"}}],
    }

    async def fetch_and_verify_hash(identifier, config_hash, action):
        return dict(config), "stored-id"

    async def get_config(identifier):
        return config, "new-hash"

    async def validate_registry_ids(*_args, **_kwargs):
        return None

    async def wait_for_unique_id(client, identifier):
        return "automation.actual"

    monkeypatch.setattr(tools, "_fetch_and_verify_hash", fetch_and_verify_hash)
    monkeypatch.setattr(tools, "_get_automation_config_internal", get_config)
    monkeypatch.setattr(
        tools_config_automations, "validate_registry_ids", validate_registry_ids
    )
    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        wait_for_unique_id,
    )

    response, _ = await tools._run_python_transform(
        "stored-id",
        "old-hash",
        "config['description'] = 'updated'",
        None,
        False,
        False,
        False,
    )

    assert response["automation_id"] == "automation.actual"
    assert response["entity_id"] == "automation.actual"
    assert response["enabled_applied"] is True
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
    client.service_error = HomeAssistantAPIError("service unavailable")
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
    assert result["enabled_requested"] is False
    assert result["enabled_applied"] is False
    assert any("config was written" in w for w in result["warnings"])


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
    client.service_error = HomeAssistantConnectionError("service unavailable")
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
    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert "service unavailable" in error["error"]["message"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_preserves_connection_errors() -> None:
    client = _FakeClient()
    client.states_error = HomeAssistantConnectionError("connection lost")
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(HomeAssistantConnectionError):
        await tools._set_runtime_only("stored-id", False, wait=False)


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
    if expected_warning is None:
        assert "warnings" not in result
    else:
        assert any(expected_warning in warning for warning in result["warnings"])


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_prefixed_identifier_must_exist() -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools._set_runtime_only("automation.missing", False, wait=False)

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
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"identifier": "automation.morning", "enabled": True}, True),
        (
            {
                "identifier": "automation.morning",
                "enabled": True,
                "take_control_of_blueprint": True,
            },
            False,
        ),
        ({"identifier": "automation.morning", "enabled": True, "config": {}}, False),
        ({"identifier": "automation.morning", "run_actions": True}, True),
        (
            {
                "identifier": "automation.morning",
                "run_actions": True,
                "python_transform": "config['alias'] = 'x'",
            },
            False,
        ),
        ({"identifier": "automation.morning", "run_actions": False}, False),
    ],
)
def test_runtime_backup_skip_excludes_config_writes(kwargs, expected) -> None:
    assert tools_config_automations._skip_automation_runtime_backup(kwargs) is expected


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


async def _no_entity(client, identifier):
    return None


class _ConfirmedReload:
    """Stand-in for config_reload_waiter whose reload is always confirmed."""

    def __init__(self, _client: Any, _event_type: str, *, enabled: bool) -> None:
        pass

    async def __aenter__(self) -> Any:
        async def wait_for_reload() -> bool:
            return True

        return wait_for_reload

    async def __aexit__(self, *_exc: Any) -> None:
        pass


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_unresolved_entity_reports_not_applied(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        _no_entity,
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

    assert client.calls == []
    assert result["enabled_requested"] is False
    assert result["enabled_applied"] is False
    assert any(
        "could not be resolved" in w and "identifier='stored-id'" in w
        for w in result["warnings"]
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_does_not_repeat_registration_poll(monkeypatch) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    polls: list[str] = []

    async def wait_for_unique_id(client, identifier) -> str | None:
        polls.append(identifier)
        return None

    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        wait_for_unique_id,
    )

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "stored-id",
        None,
        True,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        enabled=True,
    )

    assert polls == ["stored-id"]
    assert result["enabled_applied"] is False


@pytest.mark.unit
@pytest.mark.anyio
async def test_unexpected_service_exception_propagates() -> None:
    client = _FakeClient()
    client.upsert_entity_id = "automation.morning"
    client.service_error = TypeError("bug")
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(TypeError):
        await tools._run_config_update(
            {"alias": "Morning", "triggers": [], "actions": []},
            "automation.morning",
            None,
            False,
            tools_config_automations.BestPracticeCheckResult(),
            {},
            False,
            enabled=False,
        )


@pytest.mark.unit
@pytest.mark.anyio
async def test_python_transform_rejects_injected_enabled_before_write(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)

    async def fetch_and_verify_hash(identifier, config_hash, action):
        return {
            "alias": "Morning",
            "triggers": [{"trigger": "event", "event_type": "test_event"}],
            "actions": [{"action": "logbook.log", "data": {"message": "x"}}],
        }, "stored-id"

    monkeypatch.setattr(tools, "_fetch_and_verify_hash", fetch_and_verify_hash)

    with pytest.raises(ToolError) as exc_info:
        await tools._run_python_transform(
            "stored-id",
            "old-hash",
            "config['enabled'] = False",
            None,
            False,
            None,
            False,
        )

    assert "enabled" in str(exc_info.value).lower()
    assert client.upserted == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_resolves_raw_unique_id() -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools.ha_config_set_automation(
        identifier="morning-id", enabled=False, MandatoryBPS=False, wait=False
    )

    assert result["automation_id"] == "automation.morning"
    assert client.calls == [
        ("automation", "turn_off", {"entity_id": "automation.morning"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_requires_identifier() -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools._set_runtime_only(None, False, wait=False)

    assert "identifier is required" in str(exc_info.value)
    assert client.calls == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_verification_error_is_warning(monkeypatch) -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    async def wait_for_state(*_args, **_kwargs):
        raise HomeAssistantConnectionError("socket closed")

    monkeypatch.setattr(
        tools_config_automations, "wait_for_state_change", wait_for_state
    )

    result = await tools.ha_config_set_automation(
        identifier="automation.morning", enabled=True, MandatoryBPS=False, wait=True
    )

    assert result["enabled_applied"] is True
    assert any("verification failed" in w for w in result["warnings"])


class _FakeWsClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.handlers: dict[str, list[Any]] = {}
        self.unsubscribed: list[int] = []

    def add_event_handler(self, event_type: str, handler: Any) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_event_handler(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type].remove(handler)

    async def subscribe_events(self, event_type: str) -> int:
        return 7

    async def unsubscribe_events(self, subscription_id: int) -> None:
        self.unsubscribed.append(subscription_id)

    async def fire(self, event_type: str) -> None:
        for handler in list(self.handlers.get(event_type, [])):
            await handler({"event_type": event_type, "data": {}})


@pytest.mark.unit
@pytest.mark.anyio
async def test_reload_waiter_returns_true_after_reload_event(monkeypatch) -> None:
    ws = _FakeWsClient()

    async def get_ws(_client):
        return ws

    monkeypatch.setattr(util_helpers, "_get_waiter_ws_client", get_ws)

    async with util_helpers.config_reload_waiter(
        object(), "automation_reloaded"
    ) as wait_for_reload:
        await ws.fire("automation_reloaded")
        assert await wait_for_reload() is True

    assert ws.unsubscribed == [7]
    assert ws.handlers["automation_reloaded"] == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_reload_waiter_times_out_and_skips_without_ws(monkeypatch) -> None:
    ws = _FakeWsClient()

    async def get_ws(_client):
        return ws

    monkeypatch.setattr(util_helpers, "_get_waiter_ws_client", get_ws)
    async with util_helpers.config_reload_waiter(
        object(), "automation_reloaded", timeout=0.01
    ) as wait_for_reload:
        assert await wait_for_reload() is False

    async with util_helpers.config_reload_waiter(
        object(), "automation_reloaded", enabled=False
    ) as wait_for_reload:
        assert await wait_for_reload() is None


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_applies_enabled_only_after_reload(monkeypatch) -> None:
    order: list[str] = []
    client = _FakeClient()
    client.upsert_entity_id = "automation.morning"
    real_upsert = client.upsert_automation_config
    real_call_service = client.call_service

    async def upsert(*args: Any, **kwargs: Any) -> dict[str, Any]:
        order.append("write")
        return await real_upsert(*args, **kwargs)

    async def call_service(*args: Any, **kwargs: Any) -> dict[str, Any]:
        order.append("turn_off")
        return await real_call_service(*args, **kwargs)

    client.upsert_automation_config = upsert  # type: ignore[method-assign]
    client.call_service = call_service  # type: ignore[method-assign]

    class _Waiter:
        def __init__(self, _client: Any, event_type: str, *, enabled: bool) -> None:
            assert event_type == "automation_reloaded"
            self.enabled = enabled

        async def __aenter__(self) -> Any:
            order.append("subscribe")

            async def wait_for_reload() -> bool:
                order.append("reloaded")
                return False

            return wait_for_reload

        async def __aexit__(self, *_exc: Any) -> None:
            order.append("unsubscribe")

    monkeypatch.setattr(tools_config_automations, "config_reload_waiter", _Waiter)
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

    assert order == ["subscribe", "write", "reloaded", "unsubscribe", "turn_off"]
    assert any("did not confirm the automation reload" in w for w in result["warnings"])


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(("enabled", "warned"), [(False, True), (None, False)])
async def test_config_update_without_reload_subscription_warns_only_for_enabled(
    enabled, warned
) -> None:
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
        enabled=enabled,
    )

    has_warning = any(
        "could not be watched for the automation reload" in w
        for w in result.get("warnings", [])
    )
    assert has_warning is warned


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_run_actions_triggers_automation() -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools.ha_config_set_automation(
        identifier="morning-id", run_actions=True, MandatoryBPS=False, wait=False
    )

    assert result["action"] == "run_actions"
    assert result["actions_triggered"] is True
    assert "enabled" not in result
    assert client.calls == [
        ("automation", "trigger", {"entity_id": "automation.morning"})
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_then_run_actions_in_order() -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools.ha_config_set_automation(
        identifier="automation.morning",
        enabled=True,
        run_actions=True,
        MandatoryBPS=False,
        wait=False,
    )

    assert result["action"] == "set_enabled"
    assert result["enabled_applied"] is True
    assert result["actions_triggered"] is True
    assert client.calls == [
        ("automation", "turn_on", {"entity_id": "automation.morning"}),
        ("automation", "trigger", {"entity_id": "automation.morning"}),
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_run_actions_failure_raises_tool_error() -> None:
    client = _FakeClient()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    client.service_error = HomeAssistantConnectionError("service unavailable")
    tools = tools_config_automations.AutomationConfigTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_automation(
            identifier="automation.morning",
            run_actions=True,
            MandatoryBPS=False,
            wait=False,
        )

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "CONNECTION_FAILED"


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_runs_actions_after_confirmed_reload(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tools_config_automations, "config_reload_waiter", _ConfirmedReload
    )
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
        run_actions=True,
    )

    assert result["actions_triggered"] is True
    assert "enabled_applied" not in result
    assert client.calls == [
        ("automation", "trigger", {"entity_id": "automation.morning"})
    ]
    assert "warnings" not in result


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_skips_run_actions_when_reload_unconfirmed() -> None:
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
        run_actions=True,
    )

    assert client.calls == []
    assert result["actions_triggered"] is False
    assert any(
        "actions were not run" in w
        and "identifier='automation.morning', run_actions=True" in w
        for w in result["warnings"]
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_config_update_run_actions_failure_is_partial_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tools_config_automations, "config_reload_waiter", _ConfirmedReload
    )
    client = _FakeClient()
    client.upsert_entity_id = "automation.morning"
    client.service_error = HomeAssistantAPIError("service unavailable")
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools._run_config_update(
        {"alias": "Morning", "triggers": [], "actions": []},
        "automation.morning",
        None,
        False,
        tools_config_automations.BestPracticeCheckResult(),
        {},
        False,
        run_actions=True,
    )

    assert result["success"] is True
    assert result["actions_triggered"] is False
    assert any("actions could not be run" in w for w in result["warnings"])


@pytest.mark.unit
@pytest.mark.anyio
async def test_unresolved_entity_retry_names_every_requested_change(
    monkeypatch,
) -> None:
    client = _FakeClient()
    tools = tools_config_automations.AutomationConfigTools(client)
    monkeypatch.setattr(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        _no_entity,
    )
    monkeypatch.setattr(
        tools_config_automations, "config_reload_waiter", _ConfirmedReload
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
        run_actions=True,
    )

    assert client.calls == []
    assert result["actions_triggered"] is False
    assert any(
        "identifier='stored-id', enabled=False, run_actions=True" in w
        for w in result["warnings"]
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_enabled_kept_when_run_actions_then_fails() -> None:
    """A failed run after a successful enabled change is a partial success."""

    class _TriggerFails(_FakeClient):
        async def call_service(
            self, domain: str, service: str, data: dict[str, Any]
        ) -> dict[str, Any]:
            if service == "trigger":
                raise HomeAssistantAPIError("trigger failed")
            return await super().call_service(domain, service, data)

    client = _TriggerFails()
    client.states = [
        {"entity_id": "automation.morning", "attributes": {"id": "morning-id"}}
    ]
    tools = tools_config_automations.AutomationConfigTools(client)

    result = await tools.ha_config_set_automation(
        identifier="automation.morning",
        enabled=True,
        run_actions=True,
        MandatoryBPS=False,
        wait=False,
    )

    assert result["enabled_applied"] is True
    assert result["actions_triggered"] is False
    assert any("actions could not be run" in w for w in result["warnings"])
