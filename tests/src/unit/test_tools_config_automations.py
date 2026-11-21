"""Unit tests for the automation config tools on the component-absent path.

Core's config API returns the automation body as automations.yaml holds it,
so these pin that the tools pass it through untouched: ``ha_config_get_automation`` returns the
stored body verbatim and hashes it, ``python_transform`` edits that body, and
writes reach Core as given. They also pin the `automation_id` typed-id key
across get / set / remove (issues #1299, #1333, #1334) and the HA-MCP
safeguards around a write (alias-replacement guard, category gate). The
component routes are in ``test_ha_config_automation_component_routing.py``.

Everything runs on a real ``hass`` through the server's own client, without
the component's ``ha_mcp_tools/*`` commands.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient
from ha_mcp.tools.best_practice_checker import check_automation_config
from ha_mcp.tools.tools_config_automations import (
    NOT_VERIFIED_WARNING_PREFIX,
    AutomationConfigTools,
)
from ha_mcp.utils.config_hash import compute_config_hash

from ._automation_core import entity_id_of, load_automations_file, upserts
from ._command_faults import dropped_on, pooled_socket
from ._config_writes import automation_tools
from ._tool_errors import error_body
from ._tools_config_automations_core import (
    break_automations_file,
    config_reads_lost_at_entity_wait,
    deletes,
    loaded_at_second_entity_wait,
    require_vendored_skills,
    stop_loading_automations,
    store_unloaded,
    with_related_and_repairs,
)

CONFIG_ID = "abc123unique"
ENTITY_ID = "automation.morning_routine"
# Singular root keys and a legacy 'platform' trigger, as an older automation
# sits in automations.yaml: Core loads it and its config API reads it as is.
STORED: dict[str, Any] = {
    "id": CONFIG_ID,
    "alias": "Morning Routine",
    "trigger": [{"platform": "event", "event_type": "alarm_dismissed"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}],
}
STORED_HASH = compute_config_hash(STORED)
ORPHAN_ID = "orphaned_unique_id"
ORPHAN: dict[str, Any] = {**STORED, "id": ORPHAN_ID, "alias": "Orphaned"}
TRANSFORM = "config['mode'] = 'single'"
REPLACEMENT: dict[str, Any] = {
    "alias": "Bathroom lights",
    "triggers": [{"trigger": "event", "event_type": "issue_2407_test"}],
    "actions": [{"stop": "No device actions"}],
}


def _config(**changes: Any) -> dict[str, Any]:
    config = {k: v for k, v in copy.deepcopy(STORED).items() if k != "id"}
    config.update(changes)
    return config


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The tools without the component; Core has loaded ``STORED``."""
    automation_config_tools = await automation_tools(hass, core_client, monkeypatch)
    await load_automations_file(hass, "automations.yaml", [STORED])
    assert entity_id_of(hass, CONFIG_ID) == ENTITY_ID
    return automation_config_tools


async def _transform(
    tools: AutomationConfigTools, entity_id: str = CONFIG_ID, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await tools.ha_config_set_automation(
        entity_id=entity_id,
        python_transform=kwargs.pop("python_transform", TRANSFORM),
        config_hash=kwargs.pop("config_hash", STORED_HASH),
        **kwargs,
    )
    return result


async def _set(
    tools: AutomationConfigTools, *, config: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """A full-config write that returns without waiting for the entity."""
    result: dict[str, Any] = await tools.ha_config_set_automation(
        config=config, wait=False, **kwargs
    )
    return result


async def _assert_removed(client: HomeAssistantClient, config_id: str) -> None:
    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.get_automation_config(config_id)
    assert exc.value.status_code == 404


class TestGetAutomation:
    """`automation_id` canonical key and the stored body on `ha_config_get_automation`."""

    async def test_a_config_id_reads_as_the_automations_entity_id(
        self, tools: AutomationConfigTools
    ) -> None:
        """Issue #1334: one canonical typed-id key, no raw input echo."""
        result = await tools.ha_config_get_automation(entity_id=CONFIG_ID)

        assert result["success"] is True
        assert result["automation_id"] == ENTITY_ID
        assert "identifier" not in result

    async def test_an_entity_id_reads_as_itself(
        self, tools: AutomationConfigTools
    ) -> None:
        result = await tools.ha_config_get_automation(entity_id=ENTITY_ID)

        assert result["automation_id"] == ENTITY_ID

    async def test_a_config_id_core_has_not_loaded_reads_as_the_input(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        await store_unloaded(hass, [ORPHAN])

        result = await tools.ha_config_get_automation(entity_id=ORPHAN_ID)

        assert result["automation_id"] == ORPHAN_ID
        assert result["entity"] is None
        # The references warning points at related, which has nothing to show.
        assert result["related"] is None
        assert any(w.startswith("related unavailable") for w in result["warnings"])

    async def test_config_is_the_body_core_stores(
        self, tools: AutomationConfigTools
    ) -> None:
        """Rewriting root keys and 'platform' showed a config Core never
        stored, and a transform written against it edited keys that were not
        there."""
        result = await tools.ha_config_get_automation(entity_id=CONFIG_ID)

        assert result["config"] == STORED
        assert result["config_hash"] == STORED_HASH

    async def test_without_the_component_core_supplies_entity_and_related(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        """The registry row stands in for the component's lookup. References
        need the component; Core's broader Related set stands in under its
        own key (#2967) and the warning says which is which."""
        await with_related_and_repairs(hass)
        row = await core_client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": ENTITY_ID}
        )
        related = await core_client.send_websocket_message(
            {"type": "search/related", "item_type": "automation", "item_id": ENTITY_ID}
        )

        result = await tools.ha_config_get_automation(entity_id=ENTITY_ID)

        assert result["entity"] == row["result"]
        assert result["related"] == related["result"]
        assert result["validation"] is None
        assert result["references"] is None
        assert len(result["warnings"]) == 1
        assert "references" in result["warnings"][0]
        assert "related" in result["warnings"][0]

    async def test_without_the_component_a_failed_load_carries_cores_verdict(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        """Core's repair issue for this automation, not another automation's,
        stands in for the component's validation status."""
        await with_related_and_repairs(hass)
        await load_automations_file(
            hass,
            "automations.yaml",
            [
                {"id": "other", "alias": "Other", "triggers": [{"trigger": "x"}]},
                {**STORED, "trigger": [{"trigger": "x"}]},
            ],
        )

        result = await tools.ha_config_get_automation(entity_id=ENTITY_ID)

        issue = ir.async_get(hass).async_get_issue(
            "automation", f"{ENTITY_ID}_validation_failed_triggers"
        )
        assert issue is not None and issue.translation_placeholders is not None
        assert result["validation"] == {
            "status": "failed_triggers",
            "error": issue.translation_placeholders["error"],
        }

    async def test_without_the_component_a_lost_related_read_keeps_the_config(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        await with_related_and_repairs(hass)
        socket = await pooled_socket(core_client)

        with dropped_on(hass, "search/related", socket):
            result = await tools.ha_config_get_automation(entity_id=ENTITY_ID)

        assert result["config"] == STORED
        assert result["related"] is None
        assert any(w.startswith("related unavailable") for w in result["warnings"])

    async def test_without_the_component_a_refused_related_read_says_so(
        self, tools: AutomationConfigTools
    ) -> None:
        """Core without ``search`` answers ``search/related`` as unknown."""
        result = await tools.ha_config_get_automation(entity_id=ENTITY_ID)

        assert result["references"] is None
        assert result["related"] is None
        assert any(
            w.startswith("related unavailable") and "Unknown command" in w
            for w in result["warnings"]
        )


class TestPythonTransform:
    """`ha_config_set_automation` python_transform on the stored body."""

    async def test_a_transform_by_config_id_reports_the_entity_id(
        self, tools: AutomationConfigTools
    ) -> None:
        result = await _transform(tools)

        assert result["action"] == "python_transform"
        assert "identifier" not in result
        assert result["automation_id"] == ENTITY_ID

    async def test_a_transform_by_entity_id_reports_it(
        self, tools: AutomationConfigTools
    ) -> None:
        result = await _transform(tools, ENTITY_ID)

        assert result["automation_id"] == ENTITY_ID

    async def test_a_transform_of_a_config_id_core_has_not_loaded_reports_it(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        await store_unloaded(hass, [ORPHAN])

        result = await _transform(
            tools, ORPHAN_ID, config_hash=compute_config_hash(ORPHAN)
        )

        assert result["automation_id"] == ORPHAN_ID

    async def test_the_transform_edits_the_body_as_stored(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """The expression addresses the keys ha_config_get_automation showed;
        the result is sent to Core without the tool rewriting its root keys."""
        with upserts(core_client) as upsert:
            await _transform(
                tools,
                python_transform="config['trigger'][0]['event_type'] = 'alarm_snoozed'",
            )

        sent = upsert.call_args.args[0]
        assert sent["trigger"] == [{"platform": "event", "event_type": "alarm_snoozed"}]
        assert "triggers" not in sent


class TestFullConfigSetAutomationIdKey:
    """`automation_id` parity on the full-config branch, create and update."""

    async def test_a_creation_reports_the_new_entity_id(
        self, hass: HomeAssistant, tools: AutomationConfigTools
    ) -> None:
        result = await _set(tools, config=_config(alias="Evening Routine"))

        # unique_id is retained: it is HA's internal identifier, distinct from entity_id.
        assert result["automation_id"] == entity_id_of(hass, result["unique_id"])
        assert "identifier" not in result

    async def test_an_update_reports_the_entity_id(
        self, tools: AutomationConfigTools
    ) -> None:
        result = await _set(tools, entity_id=ENTITY_ID, config=_config())

        assert result["automation_id"] == ENTITY_ID
        assert "identifier" not in result

    async def test_an_entity_that_never_registers_is_named_by_its_config_id(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The automation is saved, so the caller gets its config id and a
        warning that no entity was seen, not a missing automation_id."""
        await stop_loading_automations(hass)
        # The client stops looking for the entity at once.
        monkeypatch.setattr(core_client, "POLL_BUDGET_S", 0.0)

        result = await _set(tools, config=_config(alias="Never Loaded"))

        assert result["automation_id"] == result["unique_id"]
        stored = await core_client.get_automation_config(result["unique_id"])
        assert stored["alias"] == "Never Loaded"
        assert any(
            w.startswith(NOT_VERIFIED_WARNING_PREFIX) for w in result["warnings"]
        )

    async def test_an_entity_found_by_the_second_wait_clears_the_not_verified_warning(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A recovered entity must clear the creation poll's soft-failure state."""
        await stop_loading_automations(hass)
        monkeypatch.setattr(core_client, "POLL_BUDGET_S", 0.0)

        with loaded_at_second_entity_wait(hass):
            result = await tools.ha_config_set_automation(
                config=_config(alias="Recovered"), wait=True
            )

        entity_id = entity_id_of(hass, result["unique_id"])
        assert result["automation_id"] == entity_id
        assert result["entity_id"] == entity_id
        assert all(
            NOT_VERIFIED_WARNING_PREFIX not in warning
            for warning in result.get("warnings", [])
        )

    async def test_returned_hash_locks_the_next_write(
        self, tools: AutomationConfigTools
    ) -> None:
        """The hash is of the body Core stored, so it matches the next read."""
        result = await _set(tools, entity_id=CONFIG_ID, config=_config())
        read = await tools.ha_config_get_automation(entity_id=CONFIG_ID)

        assert result["config_hash"] == read["config_hash"]

    async def test_a_failed_read_back_does_not_report_the_saved_write_as_failed(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The automation is saved; failing the call would invite a retry that
        writes it a second time."""
        with config_reads_lost_at_entity_wait(hass, core_client, monkeypatch):
            result = await _set(tools, config=_config(alias="Saved"))

        assert result["success"] is True
        assert result["config_hash"] is None
        assert any("reading it back failed" in w for w in result["warnings"])
        assert result["automation_id"] == entity_id_of(hass, result["unique_id"])


class TestAutomationUpsertResolvedThreading:
    """The tool resolves the target once and hands the REST client its config
    id with ``_resolved=True`` (issue #1813); it alone reports ``operation``
    and asks the client to wait for an entity only on a creation."""

    async def test_python_transform_threads_resolved_id(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        with upserts(core_client) as upsert:
            result = await _transform(tools, ENTITY_ID)

        args, kwargs = upsert.call_args
        assert args[1] == CONFIG_ID
        assert kwargs["_resolved"] is True
        assert kwargs["wait_for_entity"] is False
        assert result["operation"] == "updated"

    async def test_full_config_update_threads_resolved_id(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        with upserts(core_client) as upsert:
            result = await _set(tools, entity_id=ENTITY_ID, config=_config())

        args, kwargs = upsert.call_args
        assert args[1] == CONFIG_ID
        assert kwargs["_resolved"] is True
        assert kwargs["wait_for_entity"] is False
        assert result["operation"] == "updated"

    async def test_a_creation_has_no_target_and_waits_for_its_entity(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        with upserts(core_client) as upsert:
            result = await _set(tools, config=REPLACEMENT)

        args, kwargs = upsert.call_args
        assert args[1] is None
        assert kwargs["wait_for_entity"] is True
        assert result["operation"] == "created"


class TestDeleteAutomation:
    """`automation_id` parity and not-found on `ha_config_remove_automation`."""

    async def test_a_config_id_is_removed_and_reported_as_its_entity_id(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """The delete result's raw input echo must not leak into the response;
        ``unique_id`` is retained (callers track it)."""
        result = await tools.ha_config_remove_automation(
            entity_id=CONFIG_ID, wait=False
        )

        assert result["action"] == "delete"
        assert result["automation_id"] == ENTITY_ID
        assert "identifier" not in result
        assert result["unique_id"] == CONFIG_ID
        await _assert_removed(core_client, CONFIG_ID)

    async def test_an_entity_id_removes_its_config_id(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        result = await tools.ha_config_remove_automation(
            entity_id=ENTITY_ID, wait=False
        )

        assert result["automation_id"] == ENTITY_ID
        await _assert_removed(core_client, CONFIG_ID)

    async def test_a_config_id_core_has_not_loaded_is_removed_as_given(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        await store_unloaded(hass, [ORPHAN])

        result = await tools.ha_config_remove_automation(
            entity_id=ORPHAN_ID, wait=False
        )

        assert result["automation_id"] == ORPHAN_ID
        await _assert_removed(core_client, ORPHAN_ID)

    async def test_removing_a_missing_config_id_is_resource_not_found(
        self, tools: AutomationConfigTools
    ) -> None:
        """Core's DELETE answers a missing id with 400, which read as a bad
        request the caller would retry or rewrite; it names what exists."""
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_remove_automation(entity_id="gone", wait=False)

        body = error_body(exc)
        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert ENTITY_ID in body["available_automation_ids"]

    async def test_an_unresolvable_entity_id_is_not_found_before_deleting(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        with deletes(core_client) as delete, pytest.raises(ToolError) as exc:
            await tools.ha_config_remove_automation(
                entity_id="automation.gone", wait=False
            )

        assert error_body(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"
        delete.assert_not_awaited()


class TestAutomationReplacementGuard:
    """An alias change must not silently replace an unrelated automation (#2407)."""

    @pytest.mark.parametrize("entity_id", [CONFIG_ID, ENTITY_ID])
    async def test_alias_change_without_hash_does_not_write(
        self,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        entity_id: str,
    ) -> None:
        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc:
            await _set(tools, entity_id=entity_id, config=REPLACEMENT)

        error = error_body(exc)
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "Morning Routine" in str(exc.value)
        assert "Bathroom lights" in str(exc.value)
        assert "config_hash" in str(exc.value)
        assert "Omit entity_id" in str(exc.value)
        upsert.assert_not_awaited()

    async def test_dropping_the_alias_counts_as_a_change(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """Core accepts an automation without an alias, renaming it; that is a
        replacement like any other alias change."""
        config = _config()
        del config["alias"]

        with upserts(core_client) as upsert, pytest.raises(ToolError):
            await _set(tools, entity_id=CONFIG_ID, config=config)
        upsert.assert_not_awaited()

    async def test_intentional_rename_with_fresh_hash_succeeds(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        before = await tools.ha_config_get_automation(entity_id=CONFIG_ID)
        await _set(
            tools,
            entity_id=CONFIG_ID,
            config=REPLACEMENT,
            config_hash=before["config_hash"],
        )

        written = await core_client.get_automation_config(CONFIG_ID)
        assert written["alias"] == "Bathroom lights"

    async def test_a_failed_lookup_does_not_allow_an_overwrite(
        self,
        hass: HomeAssistant,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
    ) -> None:
        """A read Core fails is not a missing automation: treating it as one
        would write over whatever is stored under the id."""
        await break_automations_file(hass)

        with upserts(core_client) as upsert, pytest.raises(ToolError):
            await _set(tools, entity_id=CONFIG_ID, config=REPLACEMENT)
        upsert.assert_not_awaited()

    async def test_entity_lookup_failure_cannot_be_retried_as_creation(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """An entity_id nothing resolves is not found; a second lookup that
        recovered would write over an automation the guard never checked."""
        with (
            upserts(core_client) as upsert,
            patch.object(
                core_client, "get_entity_state", wraps=core_client.get_entity_state
            ) as reads,
            pytest.raises(ToolError) as exc,
        ):
            await _set(tools, entity_id="automation.ghost", config=REPLACEMENT)

        assert error_body(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"
        reads.assert_awaited_once_with("automation.ghost")
        upsert.assert_not_awaited()


class TestSetAutomationCategoryValidation:
    """A category that does not exist must not reach the automation.

    ``apply_entity_category`` runs after the upsert and HA accepts any category
    ID, so an unvalidated typo used to leave the automation created with a
    dangling category reference (issue #2159). Validation happens at tool entry
    so nothing is written when the category is wrong.
    """

    @pytest.fixture
    def lighting(self, hass: HomeAssistant) -> str:
        return (
            cr.async_get(hass).async_create(scope="automation", name="Lighting")
        ).category_id

    async def test_unknown_category_param_rejected_before_upsert(
        self,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        lighting: str,
    ) -> None:
        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc_info:
            await _set(tools, config=_config(), category="ghost_category")

        error_data = error_body(exc_info)
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["category"] == "ghost_category"
        assert error_data["scope"] == "automation"
        upsert.assert_not_awaited()

    async def test_unknown_category_in_config_rejected_before_upsert(
        self,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        lighting: str,
    ) -> None:
        """The config dict is the second category source — it needs the same gate."""
        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc_info:
            await _set(tools, config=_config(category="ghost_category"))

        assert error_body(exc_info)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        upsert.assert_not_awaited()

    async def test_unknown_category_rejected_on_python_transform(
        self,
        tools: AutomationConfigTools,
        core_client: HomeAssistantClient,
        lighting: str,
    ) -> None:
        """The transform path applies a category too — and must gate it too."""
        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc_info:
            await _transform(tools, category="ghost_category")

        assert error_body(exc_info)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        upsert.assert_not_awaited()

    async def test_existing_category_is_applied(
        self, hass: HomeAssistant, tools: AutomationConfigTools, lighting: str
    ) -> None:
        result = await _set(
            tools, config=_config(alias="Evening Routine"), category=lighting
        )

        entry = er.async_get(hass).async_get(result["automation_id"])
        assert entry is not None
        assert entry.categories == {"automation": lighting}


class TestBestPracticeSectionsReachTheResponse:
    """The sections a best-practice warning references reach skill_content."""

    async def test_generic_template_trigger_sections_are_attached(
        self, tools: AutomationConfigTools
    ) -> None:
        """The write tool hands the warning's referenced sections to the real
        attach_skill_content: with MandatoryBPS=False, skill_content holds
        exactly those sections, each with its text.
        """
        require_vendored_skills()
        config = _config(
            trigger=[{"platform": "template", "value_template": "{{ x == 1 }}"}]
        )
        referenced = check_automation_config(config).referenced_files
        assert len(referenced) > 1, referenced

        result = await _set(tools, config=config, MandatoryBPS=False)

        skill_content = result["skill_content"]
        assert set(skill_content) == referenced
        assert all(skill_content.values())


class TestReferenceValidationMeta:
    """What the reference validator could not check reaches the response."""

    async def test_references_left_unchecked_without_the_component_are_named(
        self, tools: AutomationConfigTools
    ) -> None:
        """Only the component can ask Core about an unsaved config; without it
        an empty warning list would read as every reference checked."""
        result = await _set(tools, config=_config(alias="Evening Routine"))

        assert "ha_mcp_tools" in result["validation"]["references_unchecked"]

    async def test_a_transform_write_is_checked_too(
        self, tools: AutomationConfigTools
    ) -> None:
        """A python_transform saved the #2989 mismatch with no validation at all."""
        result = await _transform(tools)

        assert "ha_mcp_tools" in result["validation"]["references_unchecked"]


class TestSceneCreateMisrouteThroughTheTool:
    async def test_nested_scene_create_without_a_trigger_is_refused_before_any_write(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """The guard is only worth having if the public tool runs it: a
        choose option's scene.create under an empty trigger list must be refused
        by ha_config_set_automation itself, with nothing sent to Core."""
        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_automation(
                config={
                    "alias": "Movie Snapshot",
                    "triggers": [],
                    "actions": [
                        {
                            "choose": [
                                {
                                    "conditions": [],
                                    "sequence": [
                                        {
                                            "action": "scene.create",
                                            "data": {"scene_id": "movie_night"},
                                        }
                                    ],
                                }
                            ]
                        }
                    ],
                },
                MandatoryBPS=False,
            )
        error = error_body(exc_info)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene.create" in error["message"]
        upsert.assert_not_awaited()
