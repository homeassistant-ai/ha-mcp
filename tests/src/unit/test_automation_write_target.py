"""Which automation a read, write or removal reaches, and what the tools say
when Core's config API cannot reach it.

Core's config API reads and edits only ``automations.yaml``. An automation
from YAML or a package still has a registry row (its ``id`` is the unique_id),
so the tools can tell it apart from one that does not exist. Every test runs
on a real ``hass``; the package automation is one Core loaded from a second
``automation`` key.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._automation_core import (
    COMPONENT,
    FALLBACK,
    define_in_package,
    routed_automation_tools,
    upserts,
)
from ._tool_errors import error_body

_ID = "1700000000000"
_PACKAGED = {"id": _ID, "alias": "Lights", "triggers": [], "actions": []}
_CONFIG = {"alias": "Lights", "triggers": [], "actions": []}


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


async def _refusal(
    tools: AutomationConfigTools, tool: str, **kwargs: Any
) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await getattr(tools, tool)(**kwargs)
    return error_body(exc)


# --- an automation Core's config API cannot reach --------------------------------


class TestAutomationOutsideAutomationsYaml:
    @pytest.mark.parametrize(
        ("tools", "by_entity_id"),
        [(COMPONENT, True), (FALLBACK, True), (COMPONENT, False)],
        ids=["component-entity-id", "fallback-entity-id", "component-config-id"],
        indirect=["tools"],
    )
    async def test_get_says_the_automation_is_not_editable_here(
        self, hass: HomeAssistant, tools: AutomationConfigTools, by_entity_id: bool
    ) -> None:
        """A package automation was reported as not existing although Home
        Assistant runs it, and its id was missing from the ids offered."""
        entity_id = await define_in_package(hass, _PACKAGED)

        body = await _refusal(
            tools,
            "ha_config_get_automation",
            entity_id=entity_id if by_entity_id else _ID,
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["config_id"] == _ID, body

    async def test_write_to_it_is_refused_not_created(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        """Writing would create a second automation in automations.yaml under
        the same id instead of editing the one that runs."""
        entity_id = await define_in_package(hass, _PACKAGED)

        with upserts(core_client) as upsert:
            body = await _refusal(
                tools,
                "ha_config_set_automation",
                entity_id=entity_id,
                config=dict(_CONFIG),
                wait=False,
            )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        upsert.assert_not_awaited()

    async def test_remove_says_the_automation_is_not_editable_here(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        entity_id = await define_in_package(hass, _PACKAGED)

        body = await _refusal(
            tools, "ha_config_remove_automation", entity_id=entity_id, wait=False
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND", body
        assert body["config_id"] == _ID, body
        assert hass.states.get(entity_id) is not None

    async def test_config_id_without_a_registry_row_is_not_found(
        self, tools: AutomationConfigTools
    ) -> None:
        body = await _refusal(tools, "ha_config_get_automation", entity_id=_ID)

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND", body


@pytest.mark.parametrize("tools", [FALLBACK], indirect=True)
async def test_hash_for_an_unstored_config_id_is_refused(
    tools: AutomationConfigTools, core_client: HomeAssistantClient
) -> None:
    """A config_hash comes from reading a stored automation; writing anyway
    would create a new one where the caller meant to edit."""
    with upserts(core_client) as upsert:
        body = await _refusal(
            tools,
            "ha_config_set_automation",
            entity_id="fresh_id",
            config_hash="0123456789abcdef",
            config=dict(_CONFIG),
            wait=False,
        )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND", body
    upsert.assert_not_awaited()


# --- the load failure without the component --------------------------------------


@pytest.mark.parametrize("tools", [FALLBACK], indirect=True)
@pytest.mark.parametrize(
    ("issue_suffix", "validation"),
    [
        (
            "_validation_failed_blueprint",
            {"status": "failed_blueprint", "error": "Missing input event"},
        ),
        # The issue of an automation whose entity_id extends this one's.
        ("_validation_failed_blueprint_validation_failed_blueprint", None),
    ],
    ids=["own-issue", "issue-of-an-entity-id-extending-it"],
)
async def test_load_failure_is_read_from_the_automations_own_issue(
    hass: HomeAssistant,
    tools: AutomationConfigTools,
    issue_suffix: str,
    validation: dict[str, Any] | None,
) -> None:
    """A healthy automation was reported broken, with a doubled status, when
    another automation's entity_id started with its own."""
    assert await async_setup_component(hass, "repairs", {})
    created = await tools.ha_config_set_automation(
        config={"alias": f"pv {uuid.uuid4().hex[:8]}", "triggers": [], "actions": []},
        wait=False,
    )
    entity_id = created["entity_id"]
    ir.async_create_issue(
        hass,
        "automation",
        f"{entity_id}{issue_suffix}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="validation_failed_blueprint",
        translation_placeholders={
            "edit": "/config/automation/edit/x",
            "entity_id": entity_id,
            "error": "Missing input event",
            "name": "pv",
        },
    )

    got = await tools.ha_config_get_automation(entity_id=entity_id)

    assert got["validation"] == validation
