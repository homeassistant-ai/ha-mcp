"""Diagnostics tests for empty trace results, on real Core.

The diagnostics read Core's own state, automation config, registry, repair and
trace answers: each test sets up the automation or script it describes, and the
server's client talks to the real ``hass``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.automation_registry_details import (
    REFERENCES_NOT_LOADED,
    registry_details,
)
from ha_mcp.tools.component_automation import automation_lookup
from ha_mcp.tools.tools_traces import TraceTools

_TRIGGER = {"platform": "state", "entity_id": "light.trigger"}
_ACTION = [{"event": "trace_test"}]


def _automation(automation_id: str, alias: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": automation_id,
        "alias": alias,
        "trigger": [_TRIGGER],
        "action": _ACTION,
        **extra,
    }


async def _diagnose(
    client: HomeAssistantClient, entity_id: str
) -> tuple[dict[str, Any], list[str]]:
    """The diagnostics and warnings ``ha_get_automation_traces`` returns for an
    automation or script Core holds no traces for."""
    result = await TraceTools(client).ha_get_automation_traces(entity_id=entity_id)
    diagnostics: dict[str, Any] = result["diagnostics"]
    return diagnostics, list(result.get("warnings", []))


async def _setup(hass: HomeAssistant, component: str, config: Any) -> None:
    assert await async_setup_component(hass, component, {component: config})
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
async def trace_store(hass: HomeAssistant) -> None:
    """Core's trace store, which serves ``trace/list`` and the automation store."""
    assert await async_setup_component(hass, "trace", {})


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Keep the built-in blueprints Core copies on setup out of the repository."""
    return hass_tmp_config_dir


@pytest.fixture
async def registry_api(hass: HomeAssistant) -> None:
    """Core's config component, which serves ``config/entity_registry/get``."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def repairs_api(hass: HomeAssistant) -> None:
    """Core's repairs integration, which serves ``repairs/list_issues``."""
    assert await async_setup_component(hass, "repairs", {})


class TestGatherDiagnostics:
    @pytest.mark.asyncio
    async def test_a_loaded_automation_that_never_ran_is_told_to_run_it(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """An enabled automation Core never ran has no last_triggered and is told
        to run it once, rather than that its trace storage is off."""
        await _setup(hass, "automation", [_automation("test", "Test")])
        state = hass.states.get("automation.test")
        assert state is not None
        assert state.attributes["last_triggered"] is None

        result, warnings = await _diagnose(component_core_client, "automation.test")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        assert result["trace_key_available"] is True
        assert result["trace_storage_enabled"] is True
        assert result["last_triggered"] is None
        assert "run the automation/script once" in result["suggestion"].lower()
        assert warnings == []

    @pytest.mark.asyncio
    async def test_a_ran_automation_reports_core_s_last_triggered(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """An automation that ran but keeps no traces reports Core's own
        last_triggered, so the agent knows the run happened."""
        await _setup(
            hass,
            "automation",
            [_automation("test", "Test", trace={"stored_traces": 0})],
        )
        hass.states.async_set("light.trigger", "on")
        await hass.async_block_till_done()
        state = hass.states.get("automation.test")
        assert state is not None
        last_triggered = state.attributes["last_triggered"]
        assert last_triggered is not None

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["last_triggered"] == last_triggered.isoformat()

    @pytest.mark.asyncio
    async def test_automation_without_an_id_has_no_trace_key(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _setup(
            hass,
            "automation",
            [{"alias": "No Key", "trigger": [_TRIGGER], "action": _ACTION}],
        )

        result, _warnings = await _diagnose(component_core_client, "automation.no_key")

        assert result["trace_key_available"] is False
        assert "unique_id" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_disabled_automation_is_not_told_to_enable_it(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        await _setup(hass, "automation", [_automation("test", "Test")])
        entity_registry.async_update_entity(
            "automation.test", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["entity_enabled"] is False
        assert result["loaded"] is False
        assert "enable it" not in result["suggestion"].lower()
        assert "automation.trigger" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_turned_off_loaded_automation_mentions_trigger(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """A turned-off automation is loaded; automation.trigger still runs it."""
        await _setup(
            hass,
            "automation",
            [_automation("test", "Test", initial_state=False)],
        )

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["entity_enabled"] is False
        assert result["loaded"] is True
        assert "enable it" not in result["suggestion"].lower()
        assert "automation.trigger" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_an_unavailable_automation_is_not_called_disabled(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """An unavailable entity cannot be triggered, so say that instead."""
        await _setup(hass, "automation", [_automation("test", "Test")])
        hass.states.async_set("automation.test", "unavailable")
        await hass.async_block_till_done()

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["entity_state"] == "unavailable"
        assert "unavailable" in result["suggestion"].lower()
        assert "disabled" not in result["suggestion"].lower()
        assert "automation.trigger" not in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_typo_entity_is_not_found(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        await _setup(hass, "automation", [_automation("test", "Test")])

        result, _warnings = await _diagnose(component_core_client, "automation.typo")

        assert result["entity_exists"] is False
        assert result["trace_key_available"] is False
        assert "could not find" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_stored_traces_zero_is_reported_as_storage_disabled(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """An automation with stored_traces=0 must say its trace storage is
        off, so an empty list is not read as a missing run."""
        await _setup(
            hass,
            "automation",
            [_automation("test", "Test", trace={"stored_traces": 0})],
        )

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["trace_storage_enabled"] is False
        assert "stored_traces" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_load_failure_leads_with_core_s_verdict(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Core's validation repair issue explains why there is no trace."""
        await _setup(
            hass,
            "automation",
            [
                {
                    "id": "bad",
                    "alias": "Bad",
                    "trigger": [{"platform": "no_such_platform"}],
                    "action": [],
                }
            ],
        )

        result, _warnings = await _diagnose(component_core_client, "automation.bad")

        assert result["validation"] is not None
        assert result["validation"]["status"].startswith("failed")
        assert "did not load" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_an_unreadable_config_reports_unknown(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        hass.states.async_set("automation.test", "on")

        result, _warnings = await _diagnose(component_core_client, "automation.test")

        assert result["trace_storage_enabled"] is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("registry_api")
    async def test_script_reads_the_registry_without_the_automation_component(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """The automation-only component is not reported missing for scripts."""
        await _setup(
            hass,
            "script",
            {"test": {"sequence": [{"event": "trace_test"}]}},
        )

        result, warnings = await _diagnose(component_core_client, "script.test")

        assert result["entity_exists"] is True
        assert result["trace_key_available"] is True
        assert result["trace_storage_enabled"] is True
        assert result["loaded"] is None
        assert not any("component" in warning for warning in warnings)


class TestDiagnosticsResponseWarnings:
    """Diagnostics warnings surface at the tool response level."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("registry_api", "repairs_api")
    async def test_a_missing_component_warns_without_claiming_not_loaded_or_related(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A typo names the omitted References and no load failure Core never said."""
        result = await TraceTools(core_client).ha_get_automation_traces(
            entity_id="automation.typo"
        )

        warnings = " ".join(result["warnings"])
        assert "component" in warnings
        assert "not loaded" not in warnings.lower()
        assert "references are omitted" in warnings
        assert "related holds" not in warnings

    @pytest.mark.asyncio
    async def test_a_validation_failure_warns_at_response_level(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """A non-ok validation status emits a response-level warning."""
        await _setup(
            hass,
            "automation",
            [
                {
                    "id": "bad",
                    "alias": "Bad",
                    "trigger": [{"platform": "no_such_platform"}],
                    "action": [],
                }
            ],
        )

        result = await TraceTools(component_core_client).ha_get_automation_traces(
            entity_id="automation.bad"
        )

        assert "warnings" in result
        assert any(
            "validation failed" in warning.lower() for warning in result["warnings"]
        )


class TestRegistryDetailsReferences:
    """The component's lookup answer for an item's registry row and references."""

    @pytest.mark.asyncio
    async def test_an_unknown_entity_id_is_not_reported_as_an_unloaded_one(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """A typo'd entity id must not be told the automation exists but did not
        load, which would send the user looking for a disabled automation."""
        await _setup(hass, "automation", [_automation("test", "Test")])
        lookup = await automation_lookup(
            component_core_client, entity_id="automation.typo"
        )
        assert lookup is not None
        assert lookup["found"] is False
        warnings: list[str] = []

        await registry_details(
            component_core_client, lookup, "automation.typo", warnings
        )

        assert warnings == []

    @pytest.mark.asyncio
    async def test_an_automation_that_fails_to_load_says_its_references_are_unavailable(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """Core has no references for an automation it did not load, so the user
        is told that instead of reading an unexplained miss."""
        await _setup(
            hass,
            "automation",
            [
                {
                    "id": "bad",
                    "alias": "Bad",
                    "trigger": [{"platform": "no_such_platform"}],
                    "action": [],
                }
            ],
        )
        lookup = await automation_lookup(
            component_core_client, entity_id="automation.bad"
        )
        assert lookup is not None
        assert (lookup["found"], lookup["loaded"], lookup["references"]) == (
            True,
            False,
            None,
        )
        warnings: list[str] = []

        await registry_details(
            component_core_client, lookup, "automation.bad", warnings
        )

        assert warnings == [REFERENCES_NOT_LOADED]
