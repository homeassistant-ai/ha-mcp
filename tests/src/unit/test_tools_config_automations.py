"""Unit tests for the automation config tools on the component-absent path.

Core stores and returns the automation body as written, so these pin that the
tools pass it through untouched: ``ha_config_get_automation`` returns the
stored body verbatim and hashes it, ``python_transform`` edits that body, and
writes reach Core as given. They also pin the `automation_id` typed-id key
across get / set / remove (issues #1299, #1333, #1334) and the HA-MCP
safeguards around a write (alias-replacement guard, category gate). The
component routes are in ``test_ha_config_automation_component_routing.py``.
"""

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import tools_config_automations as automations_module
from ha_mcp.tools.best_practice_checker import check_automation_config
from ha_mcp.tools.tools_config_automations import (
    NOT_VERIFIED_WARNING_PREFIX,
    AutomationConfigTools,
    _strip_redundant_identifier_echo,
)
from ha_mcp.utils.config_hash import compute_config_hash

from .test_skill_content_wiring import _require_vendored_skills

# Singular root keys and a legacy 'platform' trigger, as an older automation
# sits in automations.yaml: Core accepts and stores it like this.
STORED: dict[str, Any] = {
    "id": "abc123unique",
    "alias": "Morning Routine",
    "trigger": [{"platform": "time", "at": "07:00:00"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}],
}
STORED_HASH = compute_config_hash(STORED)


def _config(**changes: Any) -> dict[str, Any]:
    config = {k: v for k, v in copy.deepcopy(STORED).items() if k != "id"}
    config.update(changes)
    return config


@pytest.fixture
def mock_client():
    """Component-absent client satisfying read, write and delete paths."""
    client = MagicMock()
    # Credential-less, so get_component_caps answers None: the fallback path.
    client.base_url = None
    client.token = None
    client.get_automation_config = AsyncMock(return_value=copy.deepcopy(STORED))
    client._resolve_automation_id = AsyncMock(return_value="abc123unique")
    # Individual tests override .get_states for the no-match case.
    client.get_states = AsyncMock(
        return_value=[
            {
                "entity_id": "automation.morning_routine",
                "attributes": {"id": "abc123unique"},
            }
        ]
    )
    # Required by ``validate_config_references`` (asyncio.gather with get_states).
    client.get_services = AsyncMock(return_value={})
    # Registry reads answer an entry without categories.
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"categories": {}}}
    )
    client.upsert_automation_config = AsyncMock(
        return_value={
            "unique_id": "abc123unique",
            "entity_id": "automation.morning_routine",
            "result": "ok",
        }
    )
    # Delete path: the REST client echoes its input and the config id.
    client.delete_automation_config = AsyncMock(
        return_value={
            "identifier": "abc123unique",
            "unique_id": "abc123unique",
            "result": "ok",
            "operation": "deleted",
        }
    )
    return client


@pytest.fixture
def tools(mock_client):
    return AutomationConfigTools(mock_client)


class TestGetAutomation:
    """`automation_id` canonical key and the stored body on `ha_config_get_automation`."""

    async def test_returns_resolved_entity_id_when_input_is_config_id(self, tools):
        result = await tools.ha_config_get_automation(entity_id="abc123unique")

        assert result["success"] is True
        assert result["automation_id"] == "automation.morning_routine"

    async def test_returns_input_when_input_is_entity_id(self, tools):
        result = await tools.ha_config_get_automation(
            entity_id="automation.morning_routine"
        )

        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_input_when_registry_lookup_misses(
        self, tools, mock_client
    ):
        mock_client.get_states = AsyncMock(return_value=[])

        result = await tools.ha_config_get_automation(entity_id="orphaned_unique_id")

        assert result["automation_id"] == "orphaned_unique_id"
        assert result["entity"] is None

    async def test_identifier_key_no_longer_in_response(self, tools):
        """Issue #1334: one canonical typed-id key, no raw input echo."""
        result = await tools.ha_config_get_automation(entity_id="abc123unique")

        assert "identifier" not in result

    async def test_config_is_the_body_core_stores(self, tools):
        """Rewriting root keys and 'platform' showed a config Core never
        stored, and a transform written against it edited keys that were not
        there."""
        result = await tools.ha_config_get_automation(entity_id="abc123unique")

        assert result["config"] == STORED
        assert result["config_hash"] == STORED_HASH

    async def test_without_the_component_core_supplies_entity_and_validation(
        self, tools, mock_client
    ):
        """The registry row and Core's repair issue stand in for the
        component's lookup; references need the component and say so."""
        row = {"entity_id": "automation.morning_routine", "labels": ["kitchen"]}
        issues = [
            {
                "domain": "automation",
                "issue_id": "automation.other_validation_failed_schema",
                "translation_placeholders": {"error": "not this one"},
            },
            {
                "domain": "automation",
                "issue_id": "automation.morning_routine_validation_failed_triggers",
                "translation_placeholders": {"error": "Invalid trigger 'x' specified"},
            },
        ]

        async def ws(msg: dict[str, Any]) -> dict[str, Any]:
            if msg["type"] == "repairs/list_issues":
                return {"success": True, "result": {"issues": issues}}
            return {"success": True, "result": row}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws)

        result = await tools.ha_config_get_automation(
            entity_id="automation.morning_routine"
        )

        assert result["entity"] == row
        assert result["validation"] == {
            "status": "failed_triggers",
            "error": "Invalid trigger 'x' specified",
        }
        assert result["references"] is None
        assert len(result["warnings"]) == 1
        assert "references" in result["warnings"][0]


class TestPythonTransform:
    """`ha_config_set_automation` python_transform on the stored body."""

    async def test_uses_upsert_entity_id_when_returned(self, tools):
        result = await tools.ha_config_set_automation(
            entity_id="abc123unique",
            python_transform="config['mode'] = 'single'",
            config_hash=STORED_HASH,
        )

        assert result["action"] == "python_transform"
        assert "identifier" not in result
        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_entity_id_input_when_upsert_omits_it(
        self, tools, mock_client
    ):
        mock_client.upsert_automation_config.return_value = {
            "unique_id": "abc123unique",
            "entity_id": None,
            "result": "ok",
        }

        result = await tools.ha_config_set_automation(
            entity_id="automation.morning_routine",
            python_transform="config['mode'] = 'single'",
            config_hash=STORED_HASH,
        )

        assert result["automation_id"] == "automation.morning_routine"

    async def test_falls_back_to_config_id_input_when_unresolvable(
        self, tools, mock_client
    ):
        mock_client.get_states = AsyncMock(return_value=[])
        mock_client.upsert_automation_config.return_value = {
            "unique_id": "orphaned_unique_id",
            "entity_id": None,
            "result": "ok",
        }

        result = await tools.ha_config_set_automation(
            entity_id="orphaned_unique_id",
            python_transform="config['mode'] = 'single'",
            config_hash=STORED_HASH,
        )

        assert result["automation_id"] == "orphaned_unique_id"

    async def test_the_transform_edits_the_body_as_stored(self, tools, mock_client):
        """The expression addresses the keys ha_config_get_automation showed;
        the result reaches Core without its root keys being rewritten."""
        await tools.ha_config_set_automation(
            entity_id="abc123unique",
            python_transform="config['trigger'][0]['at'] = '08:00:00'",
            config_hash=STORED_HASH,
        )

        written = mock_client.upsert_automation_config.call_args.args[0]
        assert written["trigger"] == [{"platform": "time", "at": "08:00:00"}]
        assert "triggers" not in written

    async def test_a_failed_read_back_does_not_report_the_saved_write_as_failed(
        self, tools: AutomationConfigTools, mock_client
    ) -> None:
        """The transform is saved; failing the call would invite a retry that
        then meets a stale config_hash."""
        mock_client.get_automation_config = AsyncMock(
            side_effect=[
                copy.deepcopy(STORED),
                HomeAssistantAPIError("GET reply is not JSON"),
            ]
        )

        result = await tools.ha_config_set_automation(
            entity_id="abc123unique",
            python_transform="config['mode'] = 'single'",
            config_hash=STORED_HASH,
        )

        assert result["success"] is True
        assert result["config_hash"] is None
        assert any("reading it back failed" in w for w in result["warnings"])


class TestFullConfigSetAutomationIdKey:
    """`automation_id` parity on the full-config branch, create and update."""

    async def test_create_uses_upsert_entity_id_as_automation_id(self, tools):
        result = await tools.ha_config_set_automation(config=_config(), wait=False)

        assert result["automation_id"] == "automation.morning_routine"
        assert "identifier" not in result
        # unique_id retained — it's HA's internal identifier, distinct from entity_id.
        assert result.get("unique_id") == "abc123unique"

    async def test_update_uses_upsert_entity_id_as_automation_id(self, tools):
        result = await tools.ha_config_set_automation(
            entity_id="automation.morning_routine", config=_config(), wait=False
        )

        assert result["automation_id"] == "automation.morning_routine"
        assert "identifier" not in result

    async def test_create_falls_back_to_unique_id_when_entity_id_missing(
        self, tools, mock_client
    ):
        mock_client.upsert_automation_config.return_value = {
            "unique_id": "generated_unique_id_xyz",
            "entity_id": None,
            "result": "ok",
        }

        result = await tools.ha_config_set_automation(config=_config(), wait=False)

        assert result["automation_id"] == "generated_unique_id_xyz"

    async def test_create_uses_successful_unique_id_fallback_without_stale_warning(
        self, tools, mock_client, monkeypatch
    ):
        """A recovered entity must clear the creation poll's soft-failure state."""
        mock_client.upsert_automation_config.return_value = {
            "unique_id": "generated_unique_id_xyz",
            "entity_id": None,
            "entity_not_verified": True,
            "result": "ok",
        }
        monkeypatch.setattr(
            automations_module,
            "wait_for_automation_entity_by_unique_id",
            AsyncMock(return_value="automation.generated"),
        )
        monkeypatch.setattr(
            automations_module,
            "wait_for_entity_registered",
            AsyncMock(return_value=True),
        )

        result = await tools.ha_config_set_automation(config=_config(), wait=True)

        assert result["automation_id"] == "automation.generated"
        assert result["entity_id"] == "automation.generated"
        assert all(
            NOT_VERIFIED_WARNING_PREFIX not in warning
            for warning in result.get("warnings", [])
        )

    async def test_create_omits_automation_id_when_all_fallbacks_falsy(
        self, tools, mock_client
    ):
        """Defensive contract: never surface `automation_id: None`."""
        mock_client.upsert_automation_config.return_value = {
            "unique_id": None,
            "entity_id": None,
            "result": "ok",
        }

        result = await tools.ha_config_set_automation(config=_config(), wait=False)

        assert result["success"] is True
        assert "automation_id" not in result

    async def test_returned_hash_locks_the_next_write(self, tools, mock_client):
        """The hash is of the body Core stored, so it matches the next read."""
        result = await tools.ha_config_set_automation(
            entity_id="abc123unique", config=_config(), wait=False
        )
        read = await tools.ha_config_get_automation(entity_id="abc123unique")

        assert result["config_hash"] == read["config_hash"]


class TestAutomationUpsertResolvedThreading:
    """The tool resolves the target once and hands the REST client its config
    id with ``_resolved=True`` (issue #1813); it alone reports ``operation``
    and asks the client to wait for an entity only on a creation."""

    async def test_python_transform_threads_resolved_id(self, tools, mock_client):
        result = await tools.ha_config_set_automation(
            entity_id="automation.morning_routine",
            python_transform="config['mode'] = 'single'",
            config_hash=STORED_HASH,
        )

        args, kwargs = mock_client.upsert_automation_config.call_args
        assert args[1] == "abc123unique"
        assert kwargs["_resolved"] is True
        assert kwargs["wait_for_entity"] is False
        assert result["operation"] == "updated"

    async def test_full_config_update_threads_resolved_id(self, tools, mock_client):
        result = await tools.ha_config_set_automation(
            entity_id="automation.morning_routine", config=_config(), wait=False
        )

        args, kwargs = mock_client.upsert_automation_config.call_args
        assert args[1] == "abc123unique"
        assert kwargs["wait_for_entity"] is False
        assert result["operation"] == "updated"
        mock_client._resolve_automation_id.assert_awaited_once_with(
            "automation.morning_routine"
        )

    async def test_create_has_no_target(self, tools, mock_client):
        result = await tools.ha_config_set_automation(config=_config(), wait=False)

        args, kwargs = mock_client.upsert_automation_config.call_args
        assert args[1] is None
        assert kwargs["wait_for_entity"] is True
        assert result["operation"] == "created"


class TestDeleteAutomation:
    """`automation_id` parity and not-found on `ha_config_remove_automation`."""

    async def test_returns_resolved_entity_id_when_input_is_config_id(
        self, tools, mock_client
    ):
        """The delete result's raw input echo must not leak into the response;
        ``unique_id`` is retained (callers track it)."""
        result = await tools.ha_config_remove_automation(
            entity_id="abc123unique", wait=False
        )

        assert result["action"] == "delete"
        assert result["automation_id"] == "automation.morning_routine"
        assert "identifier" not in result
        assert result.get("unique_id") == "abc123unique"
        mock_client.delete_automation_config.assert_awaited_once_with("abc123unique")

    async def test_entity_id_input_deletes_its_config_id(self, tools, mock_client):
        result = await tools.ha_config_remove_automation(
            entity_id="automation.morning_routine", wait=False
        )

        assert result["automation_id"] == "automation.morning_routine"
        mock_client.delete_automation_config.assert_awaited_once_with("abc123unique")

    async def test_falls_back_to_input_when_registry_lookup_misses(
        self, tools, mock_client
    ):
        mock_client.get_states = AsyncMock(return_value=[])

        result = await tools.ha_config_remove_automation(
            entity_id="orphaned_unique_id", wait=False
        )

        assert result["automation_id"] == "orphaned_unique_id"

    async def test_removing_a_missing_config_id_is_resource_not_found(
        self, tools, mock_client
    ):
        """Core's DELETE answers a missing id with 400; reported as a failed
        validation the caller would retry or rewrite the request."""
        mock_client.delete_automation_config = AsyncMock(
            side_effect=HomeAssistantAPIError("Automation not found", status_code=404)
        )

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_remove_automation(entity_id="gone", wait=False)

        body = json.loads(str(exc.value))
        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "available_automation_ids" in body

    async def test_an_unresolvable_entity_id_is_not_found_before_deleting(
        self, tools, mock_client
    ):
        mock_client._resolve_automation_id = AsyncMock(
            side_effect=HomeAssistantAPIError("missing", status_code=404)
        )

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_remove_automation(
                entity_id="automation.gone", wait=False
            )

        assert json.loads(str(exc.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
        mock_client.delete_automation_config.assert_not_awaited()


class TestAutomationReplacementGuard:
    """An alias change must not silently replace an unrelated automation (#2407)."""

    @pytest.fixture
    def replacement(self):
        return {
            "alias": "Bathroom lights",
            "triggers": [{"trigger": "event", "event_type": "issue_2407_test"}],
            "actions": [{"stop": "No device actions"}],
        }

    @pytest.mark.parametrize(
        "entity_id", ["new", "custom_id", "automation.morning_routine"]
    )
    async def test_alias_change_without_hash_does_not_write(
        self, tools, mock_client, replacement, entity_id
    ):
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_set_automation(
                entity_id=entity_id, config=replacement, wait=False
            )

        error = json.loads(str(exc.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "Morning Routine" in str(exc.value)
        assert "Bathroom lights" in str(exc.value)
        assert "config_hash" in str(exc.value)
        assert "Omit entity_id" in str(exc.value)
        mock_client.upsert_automation_config.assert_not_awaited()

    async def test_dropping_the_alias_counts_as_a_change(self, tools, mock_client):
        """Core accepts an automation without an alias, renaming it; that is a
        replacement like any other alias change."""
        config = _config()
        del config["alias"]

        with pytest.raises(ToolError):
            await tools.ha_config_set_automation(
                entity_id="abc123unique", config=config, wait=False
            )
        mock_client.upsert_automation_config.assert_not_awaited()

    async def test_intentional_rename_with_fresh_hash_succeeds(
        self, tools, mock_client, replacement
    ):
        before = await tools.ha_config_get_automation(entity_id="abc123unique")
        await tools.ha_config_set_automation(
            entity_id="abc123unique",
            config=replacement,
            config_hash=before["config_hash"],
            wait=False,
        )
        written = mock_client.upsert_automation_config.call_args.args[0]
        assert written["alias"] == "Bathroom lights"

    @pytest.mark.parametrize("entity_id", [None, "chosen_custom_id"])
    async def test_creation_remains_available(
        self, tools, mock_client, replacement, entity_id
    ):
        mock_client.get_automation_config.side_effect = [
            HomeAssistantAPIError("Not found", status_code=404),
            copy.deepcopy(STORED),
        ]
        result = await tools.ha_config_set_automation(
            entity_id=entity_id, config=replacement, wait=False
        )
        assert result["operation"] == "created"
        args, kwargs = mock_client.upsert_automation_config.call_args
        assert args[1] == entity_id
        assert kwargs["wait_for_entity"] is True

    @pytest.mark.parametrize("status", [401, 500])
    async def test_failed_lookup_does_not_allow_overwrite(
        self, tools, mock_client, replacement, status
    ):
        mock_client.get_automation_config.side_effect = HomeAssistantAPIError(
            "Lookup failed", status_code=status
        )
        with pytest.raises(ToolError):
            await tools.ha_config_set_automation(
                entity_id="new", config=replacement, wait=False
            )
        mock_client.upsert_automation_config.assert_not_awaited()

    async def test_entity_lookup_failure_cannot_be_retried_as_creation(
        self, tools, mock_client, replacement
    ):
        """An entity_id nothing resolves is not found; a second lookup that
        recovered would write over an automation the guard never checked."""
        from ha_mcp.client.rest_client import HomeAssistantClient

        for name in ("get_entity_state", "_resolve_automation_id"):
            setattr(
                mock_client,
                name,
                getattr(HomeAssistantClient, name).__get__(mock_client),
            )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "not found"}
        )
        mock_client._request = AsyncMock(
            side_effect=[
                HomeAssistantAPIError("Not found", status_code=404),
                {"attributes": {"id": "abc123unique"}},
            ]
        )

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_set_automation(
                entity_id="automation.morning_routine", config=replacement, wait=False
            )

        assert json.loads(str(exc.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
        mock_client._request.assert_awaited_once_with(
            "GET", "/states/automation.morning_routine"
        )
        mock_client.upsert_automation_config.assert_not_awaited()


class TestStripRedundantIdentifierEcho:
    """Direct regression armor for the `_strip_redundant_identifier_echo`
    helper, pinning (a) `unique_id` survives the strip — the load-bearing
    invariant whose violation broke E2E `test_duplicate_automation_prevention`
    at 5fe5338 — and (b) `extra_excludes` actually drops the named keys
    rather than silently ignoring them.
    """

    def test_strips_identifier_retains_unique_id(self):
        """Identifier key dropped, unique_id + other keys pass through."""
        result = {"identifier": "x", "unique_id": "y", "result": "ok"}
        assert _strip_redundant_identifier_echo(result) == {
            "unique_id": "y",
            "result": "ok",
        }

    def test_extra_excludes_drops_named_keys(self):
        """extra_excludes=('success',) drops success + identifier together."""
        result = {"identifier": "x", "success": True, "unique_id": "y"}
        assert _strip_redundant_identifier_echo(
            result, extra_excludes=("success",)
        ) == {"unique_id": "y"}


class TestSetAutomationCategoryValidation:
    """A category that does not exist must not reach the automation.

    ``apply_entity_category`` runs after the upsert and HA accepts any category
    ID, so an unvalidated typo used to leave the automation created with a
    dangling category reference (issue #2159). Validation happens at tool entry
    so nothing is written when the category is wrong.
    """

    @pytest.fixture
    def canonical_config(self):
        return _config()

    @staticmethod
    def _ws_handler(*category_ids):
        """Answer the category-registry preflight; ack everything else."""

        async def handler(msg):
            if msg.get("type") == "config/category_registry/list":
                return {
                    "success": True,
                    "result": [{"category_id": cid} for cid in category_ids],
                }
            return {"success": True, "result": {"categories": {}}}

        return handler

    async def test_unknown_category_param_rejected_before_upsert(
        self, tools, mock_client, canonical_config
    ):
        mock_client.send_websocket_message.side_effect = self._ws_handler("lighting")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_automation(
                config=canonical_config, category="ghost_category", wait=False
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["category"] == "ghost_category"
        assert error_data["scope"] == "automation"
        mock_client.upsert_automation_config.assert_not_called()

    async def test_unknown_category_in_config_rejected_before_upsert(
        self, tools, mock_client, canonical_config
    ):
        """The config dict is the second category source — it needs the same gate."""
        mock_client.send_websocket_message.side_effect = self._ws_handler("lighting")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_automation(
                config={**canonical_config, "category": "ghost_category"}, wait=False
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.upsert_automation_config.assert_not_called()

    async def test_unknown_category_rejected_on_python_transform(
        self, tools, mock_client
    ):
        """The transform path applies a category too — and must gate it too."""
        mock_client.send_websocket_message.side_effect = self._ws_handler("lighting")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_automation(
                entity_id="abc123unique",
                python_transform="config['mode'] = 'single'",
                config_hash=STORED_HASH,
                category="ghost_category",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.upsert_automation_config.assert_not_called()

    async def test_existing_category_proceeds(
        self, tools, mock_client, canonical_config
    ):
        mock_client.send_websocket_message.side_effect = self._ws_handler("lighting")

        result = await tools.ha_config_set_automation(
            config=canonical_config, category="lighting", wait=False
        )

        assert result["success"] is True
        mock_client.upsert_automation_config.assert_called_once()


class TestBestPracticeSectionsReachTheResponse:
    """The sections a best-practice warning references reach skill_content."""

    async def test_generic_template_trigger_sections_are_attached(self, tools):
        """The write tool hands the warning's referenced sections to the real
        attach_skill_content: with MandatoryBPS=False, skill_content holds
        exactly those sections, each with its text.
        """
        _require_vendored_skills()
        config = _config(
            trigger=[{"platform": "template", "value_template": "{{ x == 1 }}"}]
        )
        referenced = check_automation_config(config).referenced_files
        assert len(referenced) > 1, referenced

        result = await tools.ha_config_set_automation(
            config=config, MandatoryBPS=False, wait=False
        )

        skill_content = result["skill_content"]
        assert set(skill_content) == referenced
        assert all(skill_content.values())


class TestReferenceValidationMeta:
    """What the reference validator could not check reaches the response."""

    async def test_an_unchecked_target_kind_is_named(self, tools):
        """Without the component's registry ids an area target went unchecked
        and the response read as if every reference had been checked."""
        config = _config(
            action=[{"action": "light.turn_on", "target": {"area_id": "kitchen"}}]
        )

        result = await tools.ha_config_set_automation(config=config, wait=False)

        assert result["validation"]["unchecked_target_kinds"] == ["area"]

    async def test_a_failed_reference_fetch_is_reported(self, tools, mock_client):
        mock_client.get_services = AsyncMock(side_effect=RuntimeError("down"))

        result = await tools.ha_config_set_automation(config=_config(), wait=False)

        assert result["validation"]["references_unchecked"] == "down"


class TestSceneCreateMisrouteThroughTheTool:
    async def test_nested_scene_create_without_a_trigger_is_refused_before_any_write(
        self, tools, mock_client
    ) -> None:
        """The guard is only worth having if the public tool runs it: a
        choose option's scene.create under an empty trigger list must be refused
        by ha_config_set_automation itself, with nothing sent to Core."""
        with pytest.raises(ToolError) as exc_info:
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
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene.create" in error["message"]
        mock_client.upsert_automation_config.assert_not_awaited()
