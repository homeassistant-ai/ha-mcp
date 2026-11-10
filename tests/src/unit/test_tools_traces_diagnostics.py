"""Diagnostics tests for empty trace results."""

from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_diagnostics import _gather_diagnostics

from .test_tools_traces import _dispatch, _make_pooled_client


def _client(state: dict | None, config: dict | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_entity_state.return_value = state
    client.send_websocket_message.return_value = config or {
        "success": True,
        "result": {"config": {"trace": {"stored_traces": 5}}},
    }
    return client


async def _diagnostics(
    client: AsyncMock,
    *,
    entity: dict | None,
    loaded: bool | None = True,
    validation: dict | None = None,
    domain: str = "automation",
) -> tuple[dict, list[str]]:
    lookup = {"found": entity is not None, "loaded": loaded}
    lookup_mock = AsyncMock(return_value=lookup)
    details_mock = AsyncMock(return_value=(entity, validation, None))
    with (
        patch("ha_mcp.tools.trace_diagnostics.automation_lookup", lookup_mock),
        patch("ha_mcp.tools.trace_diagnostics.registry_details", details_mock),
    ):
        result, warnings = await _gather_diagnostics(client, f"{domain}.test", domain)

    if domain == "automation":
        lookup_mock.assert_awaited_once_with(client, entity_id=f"{domain}.test")
    else:
        lookup_mock.assert_not_awaited()
    details_mock.assert_awaited_once()
    details_args, details_kwargs = details_mock.await_args
    assert details_args[1] == (lookup if domain == "automation" else None)
    assert details_args[2] == f"{domain}.test"
    assert details_kwargs["component_lookup"] is (domain == "automation")
    client.send_websocket_message.assert_awaited_once_with(
        {"type": f"{domain}/config", "entity_id": f"{domain}.test"}
    )
    return result, warnings


class TestGatherDiagnostics:
    @pytest.mark.asyncio
    async def test_loaded_automation_with_trace_key(self) -> None:
        client = _client(
            {
                "state": "on",
                "attributes": {"last_triggered": "2025-11-30T15:00:00Z"},
            }
        )
        result, _warnings = await _diagnostics(
            client, entity={"unique_id": "uid_1", "disabled_by": None}
        )

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        assert result["trace_key_available"] is True
        assert result["trace_storage_enabled"] is True
        assert result["last_triggered"] == "2025-11-30T15:00:00Z"
        assert "run the automation/script once" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_no_unique_id_does_not_claim_a_trace_key(self) -> None:
        client = _client({"state": "on", "attributes": {}})
        result, _warnings = await _diagnostics(client, entity={"disabled_by": None})

        assert result["trace_key_available"] is False
        assert "unique_id" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_disabled_automation_does_not_say_enable_it(self) -> None:
        client = _client({"state": "off", "attributes": {}})
        result, _warnings = await _diagnostics(
            client,
            entity={"unique_id": "uid_1", "disabled_by": "user"},
            loaded=False,
        )

        assert result["entity_enabled"] is False
        assert result["loaded"] is False
        assert "enable it" not in result["suggestion"].lower()
        assert "automation.trigger" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_disabled_but_loaded_automation_mentions_trigger(self) -> None:
        """A turned-off automation is loaded; automation.trigger still runs it."""
        client = _client({"state": "off", "attributes": {}})
        result, _warnings = await _diagnostics(
            client,
            entity={"unique_id": "uid_1", "disabled_by": "user"},
            loaded=True,
        )

        assert result["entity_enabled"] is False
        assert result["loaded"] is True
        assert "enable it" not in result["suggestion"].lower()
        assert "automation.trigger" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_unavailable_automation_is_not_called_disabled(self) -> None:
        """An unavailable entity cannot be triggered, so say that instead."""
        client = _client({"state": "unavailable", "attributes": {}})
        result, _warnings = await _diagnostics(
            client,
            entity={"unique_id": "uid_1"},
            loaded=True,
        )

        assert result["entity_state"] == "unavailable"
        assert "unavailable" in result["suggestion"].lower()
        assert "disabled" not in result["suggestion"].lower()
        assert "automation.trigger" not in result["suggestion"]

    @pytest.mark.asyncio
    async def test_typo_entity_is_not_found(self) -> None:
        client = _client(None)
        result, _warnings = await _diagnostics(client, entity=None, loaded=None)

        assert result["entity_exists"] is False
        assert result["trace_key_available"] is False
        assert "could not find" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_trace_storage_disabled(self) -> None:
        client = _client(
            {"state": "on", "attributes": {}},
            config={
                "success": True,
                "result": {"config": {"trace": {"stored_traces": 0}}},
            },
        )
        result, _warnings = await _diagnostics(client, entity={"unique_id": "uid_1"})

        assert result["trace_storage_enabled"] is False
        assert "stored_traces: 0" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_unreadable_config_reports_unknown(self) -> None:
        client = _client(
            {"state": "on", "attributes": {}},
            config={"success": False, "error": "unknown_command"},
        )
        result, _warnings = await _diagnostics(client, entity={"unique_id": "uid_1"})

        assert result["trace_storage_enabled"] is None

    @pytest.mark.asyncio
    async def test_script_uses_registry_fallback(self) -> None:
        client = _client({"state": "off", "attributes": {}})
        result, _warnings = await _diagnostics(
            client, entity={"unique_id": "uid_script"}, domain="script"
        )

        assert result["entity_exists"] is True
        assert result["trace_key_available"] is True

    @pytest.mark.asyncio
    async def test_script_registry_fallback_does_not_claim_missing_component(
        self,
    ) -> None:
        """The automation-only component is not reported missing for scripts."""
        client = AsyncMock()
        client.get_entity_state.return_value = {"state": "off", "attributes": {}}

        def _dispatch(message: dict) -> dict:
            message_type = message["type"]
            if message_type == "config/entity_registry/get":
                return {"success": True, "result": {"unique_id": "uid_script"}}
            if message_type == "script/config":
                return {
                    "success": True,
                    "result": {"config": {"trace": {"stored_traces": 5}}},
                }
            return {"success": False, "error": f"unexpected {message_type}"}

        client.send_websocket_message = AsyncMock(side_effect=_dispatch)

        result, warnings = await _gather_diagnostics(client, "script.test", "script")

        assert result["trace_key_available"] is True
        assert not any("component" in warning for warning in warnings)


class TestDiagnosticsResponseWarnings:
    """Diagnostics warnings surface at the tool response level."""

    @pytest.mark.asyncio
    async def test_typo_entity_warns_without_not_loaded(self) -> None:
        """A missing entity emits a response warning and never 'not loaded'."""
        client = _make_pooled_client(_dispatch())
        client.get_entity_state.return_value = None
        with patch(
            "ha_mcp.tools.trace_diagnostics.automation_lookup",
            AsyncMock(return_value=None),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.typo"
            )

        assert "warnings" in result
        assert any("component" in warning for warning in result["warnings"])
        assert not any(
            "not loaded" in warning.lower() for warning in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_validation_failure_warns_at_response_level(self) -> None:
        """A non-ok validation status emits a response-level warning."""
        client = _make_pooled_client(_dispatch())
        client.get_entity_state.return_value = {"state": "on", "attributes": {}}
        lookup = {
            "found": True,
            "loaded": False,
            "entity": {"unique_id": "uid_1"},
            "validation": {"status": "failed", "error": "bad template"},
            "references": None,
        }
        with (
            patch(
                "ha_mcp.tools.trace_diagnostics.automation_lookup",
                AsyncMock(return_value=lookup),
            ),
            patch(
                "ha_mcp.tools.trace_diagnostics.registry_details",
                AsyncMock(return_value=(lookup["entity"], lookup["validation"], None)),
            ),
        ):
            result = await TraceTools(client).ha_get_automation_traces(
                entity_id="automation.test"
            )

        assert "warnings" in result
        assert any(
            "validation failed" in warning.lower() for warning in result["warnings"]
        )
