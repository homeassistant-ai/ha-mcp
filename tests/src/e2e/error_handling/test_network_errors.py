"""
Error Handling and Edge Cases E2E Tests

Comprehensive tests for error handling, edge cases, and boundary conditions
across all MCP tools. These tests ensure robustness and proper error reporting
which is crucial for production reliability.
"""

import asyncio
import logging
import time
from typing import Any

import pytest

from ..utilities.assertions import (
    _parse_error_result,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)

# The demo lights are registry-seeded everywhere, but only the HAOS lanes
# gate boot on light.bed_light reaching the state machine — see
# _search_lights_or_tolerate_timeout for the full reasoning.
_SEEDED_LIGHT_POLL_SECONDS = 15.0
_SEEDED_LIGHT_POLL_INTERVAL = 1.0


def _get_error_str(data: dict, max_len: int = 50) -> str:
    """Extract error string from response data, handling both string and dict errors."""
    error = data.get("error", "")
    if isinstance(error, dict):
        # Structured error - extract message
        return str(error.get("message", error.get("code", str(error))))[:max_len]
    return str(error)[:max_len] if error else ""


@pytest.mark.error_handling
class TestErrorHandling:
    """Test error handling and edge cases across MCP tools."""

    async def _safe_tool_call(
        self, mcp_client, tool_name: str, params: dict[str, Any], timeout: float = 10.0
    ):
        """Safe wrapper for tool calls with timeout protection."""
        try:
            result = await asyncio.wait_for(
                mcp_client.call_tool(tool_name, params), timeout=timeout
            )
            # A tool failure reaches callers two ways depending on the
            # transport: as a raised ToolError (the except below) or as a
            # returned result carrying the error flag — ``is_error`` on
            # fastmcp's CallToolResult, ``isError`` on the raw MCP one.
            # Fold either spelling into the same marked dict, keeping the
            # structured error payload the shared parser recovers so
            # _get_error_str still finds a message, and marker-based
            # discrimination stays transport-independent.
            if getattr(result, "is_error", False) or getattr(result, "isError", False):
                return {
                    **_parse_error_result(result),
                    "success": False,
                    "tool_error": True,
                }
            return result
        except TimeoutError:
            logger.warning(f"Tool call {tool_name} timed out after {timeout}s")
            # "timed_out" lets callers tolerate the wrapper-timeout leg on
            # flaky lanes while still failing hard on a tool error. This
            # dict reaches parse_mcp_result unchanged (its passthrough
            # guard returns an already-parsed dict as-is), so the marker
            # survives; callers read it before acting on the payload and
            # settle each leg at its own decision point.
            return {
                "success": False,
                "timed_out": True,
                "error": f"Operation timed out after {timeout}s",
            }
        except Exception as e:
            logger.warning(f"Tool call {tool_name} failed: {e}")
            return {"success": False, "tool_error": True, "error": str(e)}

    async def _search_lights_once(
        self, mcp_client, limit: int
    ) -> dict[str, Any] | None:
        """Run one light search, discriminating the markers before the parse.

        Returns the parsed search payload (whose ``entities`` may legitimately
        be empty), or None on the tolerated wrapper timeout. A tool error is a
        regression and fails here rather than reaching a caller disguised as an
        empty result set.
        """
        search_result = await self._safe_tool_call(
            mcp_client,
            "ha_search",
            {"query": "light", "domain_filter": "light", "limit": limit},
        )

        if isinstance(search_result, dict) and search_result.get("timed_out"):
            return None

        assert not (
            isinstance(search_result, dict) and search_result.get("success") is False
        ), f"ha_search with valid params must not error: {search_result}"

        search_data = parse_mcp_result(search_result)
        assert search_data.get("success"), (
            f"ha_search with valid params must succeed: {search_data}"
        )
        return search_data

    async def _search_lights_or_tolerate_timeout(
        self, mcp_client, limit: int, require_entities: bool = True
    ) -> list[dict[str, Any]] | None:
        """Search for lights, tolerating only the wrapper-timeout leg.

        The markers _safe_tool_call sets do survive parse_mcp_result — its
        passthrough guard returns an already-parsed dict unchanged — but they
        only discriminate anything if someone reads them, and an unread
        tool-error dict carries no entities, so it reads exactly like "this
        instance has no lights" and silently degrades every contract
        downstream of it. Each leg is therefore settled here: the wrapper
        timeout is tolerated (None comes back, so the caller can warn and drop
        just that leg), a tool error is a regression.

        Lights are registry-seeded on EVERY backend — the demo config entry is
        baked into tests/initial_test_state/.storage/core.config_entries and
        copied in at container prep — so a successful search on a settled
        instance always has entities. The boot poll that waits for
        light.bed_light to land in the state machine
        (conftest._wait_for_haos_light_ready) runs only on the HAOS lanes,
        though, and the demo platform publishes its states after the
        integration's async_setup returns, so container / embedded lanes can
        briefly race a search that fires first (documented at
        conftest.py:1863-1874, PR #1379). With ``require_entities`` the
        bounded poll below absorbs that lag, and only a still-empty result
        after the window is a search regression rather than an empty instance.

        ``require_entities=False`` is the single-shot form for the
        service-call probe, which only needs to know whether this instance has
        a light to aim at right now: it returns a possibly-empty list and
        leaves the skip decision to the caller.

        Returns the raw entity dicts, or None on the tolerated timeout.
        """
        search_data = await self._search_lights_once(mcp_client, limit)
        if search_data is None:
            return None

        entities = search_data.get("entities") or []
        if entities or not require_entities:
            return entities

        # Empty on the first shot means the state machine may still be
        # catching up with the registry seeding, so re-poll the search on a
        # bounded window before calling it a regression.
        deadline = time.monotonic() + _SEEDED_LIGHT_POLL_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(_SEEDED_LIGHT_POLL_INTERVAL)
            search_data = await self._search_lights_once(mcp_client, limit)
            if search_data is None:
                return None
            entities = search_data.get("entities") or []
            if entities:
                return entities

        assert entities, (
            f"the registry-seeded demo lights must become searchable within "
            f"{_SEEDED_LIGHT_POLL_SECONDS:.0f}s, so a still-empty result after "
            f"the poll window is a search regression rather than an empty "
            f"instance: {search_data}"
        )
        return entities

    async def test_invalid_entity_id_handling(self, mcp_client):
        """
        Test: Invalid entity ID error handling

        Validates proper error handling when invalid entity IDs
        are provided to various MCP tools.
        """

        logger.info("❌ Testing invalid entity ID handling...")

        invalid_entity_ids = [
            "nonexistent.entity",
            "invalid_domain.test",
            "",
            "light.",
            ".invalid",
            "light.with spaces",
            "domain_with_underscore.entity-with-dashes",
        ]

        for entity_id in invalid_entity_ids:
            logger.info(f"🔍 Testing invalid entity ID: '{entity_id}'")

            # Test ha_get_state with invalid entity
            state_result = await self._safe_tool_call(
                mcp_client, "ha_get_state", {"entity_id": entity_id}
            )

            state_data = parse_mcp_result(state_result)

            # Should either fail gracefully or return not found
            if not state_data.get("success"):
                logger.info(
                    f"  ✅ Correctly failed for '{entity_id}': {_get_error_str(state_data)}"
                )
            else:
                # If it "succeeds", should indicate entity not found
                data = state_data.get("data", {})
                if not data or data.get("state") in ["unknown", "unavailable", None]:
                    logger.info(
                        f"  ✅ Correctly returned 'not found' for '{entity_id}'"
                    )
                else:
                    logger.warning(
                        f"  ⚠️ Unexpectedly found data for invalid entity '{entity_id}': {data}"
                    )

        logger.info("✅ Invalid entity ID handling test completed")

    async def test_service_call_error_handling(self, mcp_client):
        """
        Test: Service call error handling

        Tests error handling for invalid service calls including
        nonexistent services, invalid parameters, and malformed requests.
        """

        logger.info("📞 Testing service call error handling...")

        # 1. NONEXISTENT SERVICE: Call service that doesn't exist
        logger.info("🚫 Testing nonexistent service...")
        invalid_service_result = await self._safe_tool_call(
            mcp_client,
            "ha_call_service",
            {"domain": "nonexistent_domain", "service": "fake_service"},
        )

        invalid_service_data = parse_mcp_result(invalid_service_result)
        if not invalid_service_data.get("success"):
            logger.info(
                f"  ✅ Correctly failed for nonexistent service: {_get_error_str(invalid_service_data)}"
            )
        else:
            logger.warning("  ⚠️ Nonexistent service call unexpectedly succeeded")

        # 2. INVALID DOMAIN: Valid service format but invalid domain
        logger.info("🏠 Testing invalid domain...")
        invalid_domain_result = await self._safe_tool_call(
            mcp_client,
            "ha_call_service",
            {"domain": "invalid_domain", "service": "turn_on"},
        )

        invalid_domain_data = parse_mcp_result(invalid_domain_result)
        if not invalid_domain_data.get("success"):
            logger.info(
                f"  ✅ Correctly failed for invalid domain: {_get_error_str(invalid_domain_data)}"
            )

        # 3. MISSING REQUIRED PARAMETERS: Try to call service without required params
        logger.info("📋 Testing missing required parameters...")

        # Try to call light.turn_on without entity_id. The probe only means
        # something on an instance that has lights, and the helper is what
        # keeps "no lights" from standing in for a failed search. This site
        # takes the single-shot form: it is a presence check, not a contract
        # on the seeding, so an empty result skips the probe instead of
        # spending the poll window on it.
        probe_lights = await self._search_lights_or_tolerate_timeout(
            mcp_client, 1, require_entities=False
        )
        if probe_lights is None:
            logger.warning(
                "  entity search timed out; skipping the missing-params probe"
            )
        elif not probe_lights:
            logger.warning(
                "  no lights are currently searchable; skipping the "
                "missing-params probe"
            )
        else:
            # Call service without entity_id to test parameter validation
            missing_params_result = await self._safe_tool_call(
                mcp_client,
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    # Missing entity_id
                },
            )

            missing_params_data = parse_mcp_result(missing_params_result)
            # This might succeed (affects all lights) or fail depending on HA config
            logger.info(
                f"  Service call without entity_id: {'succeeded' if missing_params_data.get('success') else 'failed'}"
            )

        logger.info("✅ Service call error handling test completed")

    async def test_search_boundary_conditions(self, mcp_client):
        """
        Test: Search functionality boundary conditions

        Tests search with various edge cases including empty queries,
        extremely long queries, special characters, and limit boundaries.
        """

        logger.info("🔍 Testing search boundary conditions...")

        # 1. EMPTY QUERY: Search with empty string
        logger.info("🔳 Testing empty query...")
        empty_result = await self._safe_tool_call(
            mcp_client, "ha_search", {"query": "", "limit": 5}
        )

        empty_data = parse_mcp_result(empty_result)
        if empty_data.get("success"):
            results = empty_data.get("entities", [])
            logger.info(f"  ✅ Empty query returned {len(results)} results")
        else:
            logger.info(
                f"  ✅ Empty query correctly failed: {_get_error_str(empty_data)}"
            )

        # 2. VERY LONG QUERY: Test with extremely long search string
        logger.info("📏 Testing very long query...")
        long_query = "a" * 1000  # 1000 character query
        long_result = await self._safe_tool_call(
            mcp_client, "ha_search", {"query": long_query, "limit": 5}
        )

        long_data = parse_mcp_result(long_result)
        if long_data.get("success"):
            results = long_data.get("entities", [])
            logger.info(
                f"  ✅ Long query handled gracefully, returned {len(results)} results"
            )
        else:
            logger.info(
                f"  ✅ Long query correctly failed: {_get_error_str(long_data)}"
            )

        # 3. SPECIAL CHARACTERS: Test with various special characters
        logger.info("🔣 Testing special characters...")
        special_queries = [
            "@#$%",
            "🏠🔥💡",
            "café",
            "test\nwith\nnewlines",
            "query;with;semicolons",
        ]

        for query in special_queries:
            special_result = await self._safe_tool_call(
                mcp_client, "ha_search", {"query": query, "limit": 5}
            )

            special_data = parse_mcp_result(special_result)
            status = "succeeded" if special_data.get("success") else "failed"
            logger.info(f"  Query '{query}': {status}")

        # 4. EXTREME LIMITS: Test boundary limit values
        logger.info("🔢 Testing extreme limit values...")
        extreme_limits = [0, -1, 1000000, 9999]

        for limit in extreme_limits:
            limit_result = await self._safe_tool_call(
                mcp_client, "ha_search", {"query": "light", "limit": limit}
            )

            limit_data = parse_mcp_result(limit_result)
            if limit_data.get("success"):
                results = limit_data.get("entities", [])
                logger.info(f"  Limit {limit}: returned {len(results)} results")
            else:
                logger.info(
                    f"  Limit {limit}: failed - {limit_data.get('error', '')[:30]}"
                )

        logger.info("✅ Search boundary conditions test completed")

    async def test_template_error_conditions(self, mcp_client):
        """
        Test: Template evaluation error conditions

        Tests template evaluation with invalid syntax, undefined variables,
        circular references, and other error conditions.
        """

        logger.info("🧪 Testing template error conditions...")

        error_templates = [
            # Syntax errors
            ("{{ invalid syntax", "Invalid syntax"),
            ("{{ missing_end_brace", "Missing end brace"),
            ("{{{{ too_many_braces }}}}", "Too many braces"),
            # Undefined variables
            ("{{ nonexistent_variable }}", "Undefined variable"),
            ("{{ states.nonexistent.entity }}", "Nonexistent entity"),
            # Invalid functions
            ("{{ invalid_function() }}", "Invalid function"),
            ("{{ states().nonexistent_method() }}", "Invalid method"),
            # Type errors
            ("{{ 'string' + 123 }}", "Type mismatch"),
            ("{{ states('light.test').invalid_attribute }}", "Invalid attribute"),
        ]

        for template, description in error_templates:
            logger.info(f"🧪 Testing {description}: {template[:30]}...")

            template_result = await self._safe_tool_call(
                mcp_client, "ha_eval_template", {"template": template}
            )

            template_data = parse_mcp_result(template_result)

            if not template_data.get("success"):
                error_msg = template_data.get("error", "No error message")
                logger.info(f"  ✅ Correctly failed: {error_msg[:50]}")
            else:
                result = template_data.get("result", "")
                logger.warning(
                    f"  ⚠️ Template unexpectedly succeeded with result: {result}"
                )

        logger.info("✅ Template error conditions test completed")

    async def _assert_dispatched_status_contract(
        self, mcp_client, operation_ids: list[str]
    ) -> None:
        """Assert live status for the ids a bulk batch just dispatched.

        Fabricated entities never appear in this list because the batch keeps
        the validate_first=True default: control_device_smart validates the
        entity before store_pending_operation registers an id, so a rejected
        entity never gets one. Every id present therefore has to resolve to a
        live per-item entry whose status is one a dispatched operation can
        actually reach — never not_found, which would mean the batch lost
        track of an operation it just created.

        The not_found VALUE contract for ids that were never dispatched is
        pinned separately by test_operation_status.py's
        test_list_operation_ids_invalid.
        """
        # Keep the poll window below _safe_tool_call's 10s wrapper timeout so
        # a still-pending operation returns a pending entry instead of
        # tripping the wrapper.
        status_result = await self._safe_tool_call(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": operation_ids, "timeout_seconds": 5},
        )

        # Same discrimination as the bulk call: tolerate only the wrapper
        # timeout; a failed status call on ids the batch just created is a
        # regression, not a skip.
        if isinstance(status_result, dict) and status_result.get("timed_out"):
            logger.warning("  status call timed out; tolerated on flaky lanes")
            return

        assert not (
            isinstance(status_result, dict) and status_result.get("success") is False
        ), f"bulk status must not fail on ids the batch just created: {status_result}"
        status_data = parse_mcp_result(status_result)
        detailed = status_data.get("detailed_results", [])
        # Count first. The id-set comparison below collapses duplicates, so
        # only a key-set-preserving duplication (the same id reported twice,
        # or an extra keyless entry alongside a full set) slips past it —
        # gather builds exactly one entry per id today, so treat this as a
        # forward regression guard on that invariant rather than a live bug.
        assert len(detailed) == len(operation_ids), (
            f"bulk status must return one entry per dispatched id, got "
            f"{len(detailed)} for {len(operation_ids)} ids: {status_data}"
        )
        by_status = {
            entry.get("operation_id"): entry.get("status") for entry in detailed
        }
        assert set(by_status) == set(operation_ids), (
            f"bulk status must cover exactly the dispatched ids: {status_data}"
        )
        # A failed entry carries the structured payload of the single-op
        # error it was folded back from, and the only code that maps to
        # "failed" on purpose is SERVICE_CALL_FAILED — anything else lands
        # there through the unknown-code fallback and would hide what
        # actually went wrong.
        for entry in detailed:
            if entry.get("status") == "failed":
                assert entry.get("error", {}).get("code") == "SERVICE_CALL_FAILED", (
                    f"a failed operation must report SERVICE_CALL_FAILED, not "
                    f"an unmapped code folded into the same status: {entry}"
                )
        stray = {
            op_id: status
            for op_id, status in by_status.items()
            if status not in ("completed", "pending", "timeout", "failed")
        }
        assert not stray, (
            f"dispatched operations reported an impossible status "
            f"(batch lost track of them): {stray}"
        )
        logger.info(f"    dispatched statuses: {by_status}")

    async def _assert_empty_batch_rejected(self, mcp_client) -> None:
        """Assert an empty operations list is rejected rather than accepted.

        bulk_device_control raises before touching any entity when the list is
        empty (VALIDATION_MISSING_PARAMETER, "No operations provided"), so the
        wrapper hands back an explicit ``success: False``. An accepted empty
        batch would mean the guard is gone and a caller's typo'd payload now
        reports a clean no-op run.
        """
        empty_bulk_result = await self._safe_tool_call(
            mcp_client, "ha_bulk_control", {"operations": []}
        )

        if isinstance(empty_bulk_result, dict) and empty_bulk_result.get("timed_out"):
            logger.warning("  empty-batch call timed out; tolerated on flaky lanes")
            return

        empty_bulk_data = parse_mcp_result(empty_bulk_result)
        assert empty_bulk_data.get("success") is False, (
            f"an empty operations list must be rejected, not accepted as a "
            f"no-op batch: {empty_bulk_data}"
        )
        logger.info(
            f"  ✅ Empty entity list correctly failed: {_get_error_str(empty_bulk_data)}"
        )

    async def _assert_invalid_action_rejected(
        self, mcp_client, valid_entities: list[str]
    ) -> None:
        """Assert an invalid action fails every item and dispatches nothing.

        The action check runs before anything is dispatched:
        control_device_smart raises SERVICE_INVALID_ACTION as soon as the
        action is outside the domain handler's valid_actions, ahead of
        _build_service_call and of store_pending_operation, and the component
        tier bails the whole batch back to legacy at resolution time for the
        same reason. So every item lands in the response as a per-item failure
        and no service call is ever dispatched — which is exactly what a
        vocabulary drift would quietly undo.
        """
        invalid_action_result = await self._safe_tool_call(
            mcp_client,
            "ha_bulk_control",
            {
                "operations": [
                    {"entity_id": entity_id, "action": "invalid_action"}
                    for entity_id in valid_entities
                ]
            },
        )

        if isinstance(invalid_action_result, dict) and invalid_action_result.get(
            "timed_out"
        ):
            logger.warning("  invalid-action call timed out; tolerated on flaky lanes")
            return

        invalid_action_data = parse_mcp_result(invalid_action_result)
        assert invalid_action_data.get("failed_commands") == len(valid_entities), (
            f"every operation carrying an invalid action must fail as a "
            f"per-item entry ({len(valid_entities)} expected): "
            f"{invalid_action_data}"
        )
        assert invalid_action_data.get("successful_commands") == 0, (
            f"an invalid action is rejected before dispatch, so nothing may "
            f"report success: {invalid_action_data}"
        )
        logger.info(
            f"  ✅ Invalid action correctly failed per item: "
            f"{invalid_action_data.get('failed_commands')} failed, "
            f"{invalid_action_data.get('successful_commands')} succeeded"
        )

    async def test_bulk_operation_error_scenarios(self, mcp_client):
        """
        Test: Bulk operation error scenarios

        Tests bulk operations with invalid entity lists, mixed valid/invalid entities,
        and other error conditions specific to bulk operations.
        """

        logger.info("📦 Testing bulk operation error scenarios...")

        # 1. EMPTY ENTITY LIST: Bulk operation with no entities
        logger.info("🔳 Testing empty entity list...")
        await self._assert_empty_batch_rejected(mcp_client)

        # 2. INVALID ENTITIES: Mix of valid and invalid entity IDs
        logger.info("❌ Testing mixed valid/invalid entities...")

        # Get one valid entity. An empty valid_entities degrades every
        # contract below (nothing dispatches, so no operation ids and no
        # status section), which is why the helper only lets the tolerated
        # search timeout produce one.
        found = await self._search_lights_or_tolerate_timeout(mcp_client, 1)
        if found is None:
            logger.warning(
                "  entity search timed out; proceeding with fabricated entities only"
            )
            valid_entities: list[str] = []
        else:
            valid_entities = [found[0]["entity_id"]]

        # Always non-empty: the two fabricated ids are unconditional.
        mixed_entities = valid_entities + ["nonexistent.entity", "invalid.test"]

        mixed_bulk_result = await self._safe_tool_call(
            mcp_client,
            "ha_bulk_control",
            {
                "operations": [
                    # "on" is the control-action vocabulary
                    # (valid_actions tables); "turn_on" is rejected
                    # upfront for every entity, which kept this whole
                    # block dead.
                    {"entity_id": entity_id, "action": "on"}
                    for entity_id in mixed_entities
                ]
            },
        )

        # A wholesale batch failure is the exact outcome this block
        # exists to rule out, so it must FAIL the test, not skip it:
        # an aborted batch arrives as a ToolError (folded into a
        # plain dict by _safe_tool_call), never as a response with
        # per-item entries. Only the wrapper-timeout leg is tolerated
        # (flaky lanes), and the markers make the two legs tellable
        # apart — parse_mcp_result hands a wrapper dict back unchanged,
        # so read them here rather than after the parse.
        if isinstance(mixed_bulk_result, dict) and mixed_bulk_result.get("timed_out"):
            logger.warning("  bulk call timed out; tolerated on flaky lanes")
        else:
            assert not (
                isinstance(mixed_bulk_result, dict)
                and mixed_bulk_result.get("success") is False
            ), (
                f"bulk_device_control must not abort wholesale on a "
                f"batch containing invalid entities: {mixed_bulk_result}"
            )
            mixed_bulk_data = parse_mcp_result(mixed_bulk_result)
            # The BATCH SHAPE is what routes this call to the LEGACY
            # dispatch path, independent of whether a valid entity is in
            # it: _build_service_call keeps each entity's own domain, so
            # the fabricated IDs resolve to unknown services
            # (nonexistent.turn_on), the component runs all guards before
            # any dispatch and the whole frame raises, and
            # _bulk_via_component treats that as a fallback signal — so
            # legacy serves the batch and any dispatched entity gets a
            # polling handle.
            #
            # Value-check the per-item contract on the response: the
            # valid entities succeed and both fabricated entities fail
            # as structured per-item entries (a missing entity becomes
            # ENTITY_NOT_FOUND inside the batch) instead of aborting it.
            operation_ids = mixed_bulk_data.get("operation_ids", [])
            assert mixed_bulk_data.get("successful_commands") == len(valid_entities), (
                f"the valid entities must succeed: {mixed_bulk_data}"
            )
            assert mixed_bulk_data.get("failed_commands") == 2, (
                f"both fabricated entities must fail as per-item entries "
                f"(not abort the batch): {mixed_bulk_data}"
            )
            if valid_entities:
                assert len(operation_ids) == len(valid_entities), (
                    f"the status contract below must be consciously updated, "
                    f"not silently skipped: {mixed_bulk_data}"
                )
            logger.info(
                f"  ✅ Mixed entities handled: "
                f"{mixed_bulk_data.get('successful_commands')} succeeded, "
                f"{mixed_bulk_data.get('failed_commands')} failed, "
                f"{len(operation_ids)} operation ids"
            )

            # Legacy tier only: dispatched operations get polling
            # handles — the assert above pins that count, so this gate
            # covers only the tolerated search-timeout leg, where no
            # valid entity dispatched and the list is legitimately empty.
            if operation_ids:
                await self._assert_dispatched_status_contract(mcp_client, operation_ids)

        # 3. INVALID ACTION: Bulk operation with invalid action.
        # Gated on valid_entities because the tolerated search-timeout leg
        # leaves nothing to aim the invalid action at.
        logger.info("🎬 Testing invalid action...")
        if valid_entities:
            await self._assert_invalid_action_rejected(mcp_client, valid_entities)

        logger.info("✅ Bulk operation error scenarios test completed")

    async def test_helper_creation_validation(self, mcp_client, cleanup_tracker):
        """
        Test: Helper creation validation and error handling

        Tests helper creation with invalid configurations, missing required fields,
        and constraint violations.
        """

        logger.info("🔧 Testing helper creation validation...")

        # 1. MISSING REQUIRED FIELDS: Try to create helper without name
        logger.info("📝 Testing missing required fields...")
        try:
            missing_name_result = await self._safe_tool_call(
                mcp_client,
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    # Missing name - should fail at FastMCP validation level
                },
            )
            missing_name_data = parse_mcp_result(missing_name_result)
            if not missing_name_data.get("success"):
                logger.info(
                    f"  ✅ Missing name correctly failed: {_get_error_str(missing_name_data)}"
                )
            else:
                logger.warning("  ⚠️ Missing name unexpectedly succeeded")
        except Exception as e:
            error_str = str(e).lower()
            if any(
                phrase in error_str
                for phrase in [
                    "required property",
                    "validation error",
                    "missing required parameter",
                    "missing",
                    "required",
                    "name",
                ]
            ):
                logger.info(
                    f"  ✅ Missing name correctly failed at validation: {str(e)[:100]}"
                )
            else:
                # Log but don't re-raise - this is an error handling test
                logger.warning(f"  ⚠️ Unexpected validation error: {str(e)}")

        # 2. INVALID HELPER TYPE: Create helper with nonexistent type
        logger.info("🔧 Testing invalid helper type...")
        invalid_type_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "nonexistent_type", "name": "Test Invalid Type"},
        )

        invalid_type_data = parse_mcp_result(invalid_type_result)
        if not invalid_type_data.get("success"):
            logger.info(
                f"  ✅ Invalid type correctly failed: {_get_error_str(invalid_type_data)}"
            )
        else:
            logger.warning("  ⚠️ Invalid helper type unexpectedly succeeded")

        # 3. CONSTRAINT VIOLATIONS: Test specific helper constraints

        # input_number with invalid range
        logger.info("🔢 Testing input_number constraint violations...")
        invalid_range_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "Test Invalid Range",
                "min_value": 100.0,
                "max_value": 50.0,  # max_value < min_value - should fail validation
                "step": 1.0,
                "mode": "slider",
            },
        )

        invalid_range_data = parse_mcp_result(invalid_range_result)
        if not invalid_range_data.get("success"):
            logger.info(
                f"  ✅ Invalid range correctly failed: {_get_error_str(invalid_range_data)}"
            )
        else:
            logger.warning("  ⚠️ Invalid range unexpectedly succeeded")

        # input_select with empty options
        logger.info("📋 Testing input_select with empty options...")
        empty_options_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": "Test Empty Options",
                "options": [],
            },
        )

        empty_options_data = parse_mcp_result(empty_options_result)
        if not empty_options_data.get("success"):
            logger.info(
                f"  ✅ Empty options correctly failed: {_get_error_str(empty_options_data)}"
            )
        else:
            logger.warning("  ⚠️ Empty options unexpectedly succeeded")

        # input_datetime with neither has_date nor has_time
        logger.info("📅 Testing input_datetime without date or time...")
        no_date_time_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "Test No Date Time",
                "has_date": False,
                "has_time": False,
            },
        )

        no_date_time_data = parse_mcp_result(no_date_time_result)
        if not no_date_time_data.get("success"):
            logger.info(
                f"  ✅ No date/time correctly failed: {_get_error_str(no_date_time_data)}"
            )
        else:
            logger.warning("  ⚠️ No date/time unexpectedly succeeded")

        logger.info("✅ Helper creation validation test completed")

    async def test_concurrent_operation_handling(self, mcp_client, cleanup_tracker):
        """
        Test: Concurrent operation handling

        Tests system behavior under concurrent load and ensures
        proper handling of simultaneous operations.
        """

        logger.info("🚀 Testing concurrent operation handling...")

        # Get some test entities. An early return here no-ops the whole
        # test, so only the tolerated search timeout is allowed to take it.
        found = await self._search_lights_or_tolerate_timeout(mcp_client, 3)
        if found is None:
            logger.warning("  entity search timed out; tolerated on flaky lanes")
            return

        entities = found[:3]

        await _run_concurrent_individual_operations(mcp_client, entities)

        await _run_concurrent_bulk_operations(mcp_client, entities)

        await _run_concurrent_helper_creation(mcp_client, cleanup_tracker)

        logger.info("✅ Concurrent operation handling test completed")


async def _safe_tool_call_standalone(
    mcp_client, tool_name: str, params: dict[str, Any] = None, timeout: float = 10.0
):
    """Standalone safe wrapper for tool calls with timeout protection."""
    if params is None:
        params = {}
    try:
        result = await asyncio.wait_for(
            mcp_client.call_tool(tool_name, params), timeout=timeout
        )
        # Marker parity with the method twin: a returned error-flagged result
        # and a raised ToolError are the same failure over different
        # transports, and callers discriminate on the markers, not the shape.
        # fastmcp spells the flag ``is_error``, the raw MCP type ``isError``.
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            return {
                **_parse_error_result(result),
                "success": False,
                "tool_error": True,
            }
        return result
    except TimeoutError:
        logger.warning(f"Tool call {tool_name} timed out after {timeout}s")
        return {
            "success": False,
            "timed_out": True,
            "error": f"Operation timed out after {timeout}s",
        }
    except Exception as e:
        logger.warning(f"Tool call {tool_name} failed: {e}")
        return {"success": False, "tool_error": True, "error": str(e)}


def _hard_failures(results: list[Any]) -> list[Any]:
    """Pick the concurrent results that failed for an untolerated reason.

    A raised exception always counts. Among returned dicts only an explicit
    ``success: False`` does — a missing key is not a failure, because several
    healthy tool payloads simply do not carry one. The bulk response is the
    family that matters here: it reports per-item counts and no top-level
    ``success`` at all, so a plain truthiness test would flag every healthy
    batch. The wrapper timeout stays the one tolerated failure class on flaky
    lanes, matching the sequential call sites above.
    """
    return [
        r
        for r in results
        if isinstance(r, Exception)
        or (
            isinstance(r, dict) and r.get("success") is False and not r.get("timed_out")
        )
    ]


async def _run_concurrent_individual_operations(mcp_client, entities):
    """Fire simultaneous update_entity service calls and log the success count."""
    logger.info("🔄 Testing concurrent individual operations...")

    async def call_service_for_entity(entity):
        """Helper function to call service for an entity."""
        try:
            result = await _safe_tool_call_standalone(
                mcp_client,
                "ha_call_service",
                {
                    "domain": "homeassistant",
                    "service": "update_entity",
                    "entity_id": entity["entity_id"],
                },
            )
            return parse_mcp_result(result)
        except Exception as e:
            logger.warning(
                f"Service call failed for {entity.get('entity_id', 'unknown')}: {e}"
            )
            return {"success": False, "error": str(e)}

    # Execute concurrent operations
    concurrent_tasks = [call_service_for_entity(entity) for entity in entities]
    concurrent_results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)

    hard_failures = _hard_failures(concurrent_results)
    assert not hard_failures, (
        f"concurrent service calls must not fail outside the tolerated "
        f"wrapper timeout: {hard_failures}"
    )

    successful_ops = sum(
        1
        for result in concurrent_results
        if isinstance(result, dict) and result.get("success")
    )
    logger.info(
        f"  ✅ {successful_ops}/{len(entities)} concurrent operations succeeded"
    )


async def _run_concurrent_bulk_operations(mcp_client, entities):
    """Run multiple bulk operations simultaneously and log the success count."""
    logger.info("📦 Testing concurrent bulk operations...")

    entity_groups = [
        [entities[0]["entity_id"]] if len(entities) > 0 else [],
        [entities[1]["entity_id"]] if len(entities) > 1 else [],
    ]

    async def bulk_operation(entity_list, action):
        """Helper function for bulk operation."""
        if not entity_list:
            return {"success": False, "error": "No entities"}
        try:
            result = await _safe_tool_call_standalone(
                mcp_client,
                "ha_bulk_control",
                {
                    "operations": [
                        {"entity_id": entity_id, "action": action}
                        for entity_id in entity_list
                    ]
                },
            )
            return parse_mcp_result(result)
        except Exception as e:
            logger.warning(f"Bulk operation failed for action {action}: {e}")
            return {"success": False, "error": str(e)}

    bulk_tasks = [
        # "on"/"off" is the control-action vocabulary (valid_actions
        # tables); "turn_on"/"turn_off" is rejected per item at action
        # validation, before anything dispatches — control_device_smart
        # raises SERVICE_INVALID_ACTION ahead of _build_service_call, and
        # the component tier bails the batch to legacy at resolution time
        # for the same reason. That kept this helper exercising nothing
        # while reporting success.
        bulk_operation(entity_groups[0], "on"),
        bulk_operation(entity_groups[1] if len(entity_groups) > 1 else [], "off"),
    ]

    bulk_results = await asyncio.gather(*bulk_tasks, return_exceptions=True)

    hard_failures = [
        r
        for r in _hard_failures(bulk_results)
        # An empty entity group is a fixture shortfall, not a tool failure:
        # bulk_operation returns this sentinel without calling anything.
        if not (isinstance(r, dict) and r.get("error") == "No entities")
    ]
    assert not hard_failures, (
        f"concurrent bulk operations must not fail outside the tolerated "
        f"wrapper timeout: {hard_failures}"
    )

    # A wholesale failure is not the only quiet leg: an invalid action (or a
    # vocabulary drift) fails PER ITEM inside an otherwise healthy-looking
    # response, so pin the per-item counts on every batch that dispatched.
    # Compare against 0 rather than testing truthiness: a dispatched batch
    # always reports the key, so a MISSING one means the response is not the
    # shape this assert believes it is checking, and must flag rather than
    # slip through as "no failures".
    item_failures = {
        i: r.get("failed_commands")
        for i, r in enumerate(bulk_results)
        if isinstance(r, dict)
        and not r.get("timed_out")
        and r.get("error") != "No entities"
        and r.get("failed_commands") != 0
    }
    assert not item_failures, (
        f"concurrent bulk operations on seeded lights must not fail "
        f"per-item: {item_failures} in {bulk_results}"
    )

    # A dispatched batch reports per-item counts and carries no top-level
    # success key, so "succeeded" here means "came back without an explicit
    # failure" — counting result.get("success") would report 0 of 2 on a
    # perfectly healthy run.
    successful_bulk = sum(
        1 for r in bulk_results if isinstance(r, dict) and r.get("success") is not False
    )
    logger.info(
        f"  ✅ {successful_bulk}/{len(bulk_tasks)} concurrent bulk operations succeeded"
    )


async def _run_concurrent_helper_creation(mcp_client, cleanup_tracker):
    """Create multiple helpers simultaneously and log the success count."""
    logger.info("🔧 Testing concurrent helper creation...")

    async def create_helper(helper_name, helper_type):
        """Helper function to create a helper."""
        try:
            result = await _safe_tool_call_standalone(
                mcp_client,
                "ha_config_set_helper",
                {"helper_type": helper_type, "name": helper_name},
            )
            data = parse_mcp_result(result)
            if data.get("success"):
                # Track for cleanup
                entity_id = (
                    data.get("entity_id")
                    or f"{helper_type}.{helper_name.lower().replace(' ', '_')}"
                )
                if hasattr(cleanup_tracker, "track"):
                    cleanup_tracker.track("helper", entity_id)
            return data
        except Exception as e:
            logger.warning(f"Helper creation failed for {helper_name}: {e}")
            return {"success": False, "error": str(e)}

    helper_tasks = [
        create_helper("Concurrent Test 1", "input_boolean"),
        create_helper("Concurrent Test 2", "input_boolean"),
        create_helper("Concurrent Test 3", "input_text"),
    ]

    helper_results = await asyncio.gather(*helper_tasks, return_exceptions=True)

    hard_failures = _hard_failures(helper_results)
    assert not hard_failures, (
        f"concurrent helper creations must not fail outside the tolerated "
        f"wrapper timeout: {hard_failures}"
    )

    successful_helpers = sum(
        1
        for result in helper_results
        if isinstance(result, dict) and result.get("success")
    )
    logger.info(
        f"  ✅ {successful_helpers}/{len(helper_tasks)} concurrent helper creations succeeded"
    )


@pytest.mark.error_handling
async def test_system_resilience_under_load(mcp_client):
    """
    Test: System resilience under load

    Tests system behavior under sustained load to ensure
    stability and proper resource management.
    """

    logger.info("💪 Testing system resilience under load...")

    # Rapid sequence of operations with timeout protection
    logger.info("⚡ Testing rapid operation sequence...")

    async def safe_overview_call():
        """Safe overview call with timeout."""
        return await _safe_tool_call_standalone(mcp_client, "ha_get_overview", {}, 15.0)

    rapid_operations = [
        safe_overview_call() for _ in range(10)
    ]  # Reduced to 10 for stability

    start_time = time.monotonic()
    rapid_results = await asyncio.gather(*rapid_operations, return_exceptions=True)
    end_time = time.monotonic()

    successful_rapid = sum(
        1
        for result in rapid_results
        if not isinstance(result, Exception)
        and parse_mcp_result(result).get("success", False)
    )
    duration = end_time - start_time

    logger.info(
        f"  ✅ {successful_rapid}/10 rapid operations succeeded in {duration:.2f}s"
    )
    logger.info(f"  📊 Average time per operation: {duration / 10:.3f}s")

    # Memory and resource usage monitoring (basic)
    logger.info("🧠 Checking system responsiveness after load...")

    # Simple responsiveness check with timeout
    try:
        response_check = await _safe_tool_call_standalone(
            mcp_client, "ha_get_overview", {}, 30.0
        )
        response_data = parse_mcp_result(response_check)

        if response_data.get("success"):
            logger.info("  ✅ System remains responsive after load test")
        else:
            logger.warning(
                f"  ⚠️ System responsiveness degraded: {response_data.get('error', '')}"
            )
    except Exception as e:
        logger.warning(f"  ⚠️ System responsiveness check failed: {e}")

    logger.info("✅ System resilience under load test completed")
