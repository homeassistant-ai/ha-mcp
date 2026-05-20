"""
E2E regression coverage for #1152 — WS-event-driven entity-registration wait.

Before #1152, ``ha_config_set_helper`` (and the other set tools) polled REST
for entity registration after their write. On a slow HA instance the poll
sometimes timed out before the entity hydrated, surfacing a soft-failure
warning in the tool response:

    "Helper created but input_number.foo not yet queryable. It may take a
    moment to become available."

The fix routes ``util_helpers.wait_for_entity_registered`` (and siblings)
through a WebSocket ``state_changed`` / ``entity_registry_updated``
subscription, then re-samples REST after the event arrives. This file
exercises the happy path against the real test HA so a regression in
either the WS subscription path or the REST fallback surfaces as a test
failure.

The tests are tagged ``config`` so the existing ``config`` lane in CI picks
them up alongside the other helper CRUD tests.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success
from ...utilities.wait_helpers import wait_for_entity_registration

logger = logging.getLogger(__name__)


def _entity_id_from_response(data: dict, helper_type: str) -> str | None:
    """Mirror the helper-CRUD test's response shape extractor (issue #1293)."""
    entity_id = data.get("entity_id")
    if not entity_id:
        helper_id = data.get("data", {}).get("id")
        if helper_id:
            entity_id = f"{helper_type}.{helper_id}"
    return entity_id


@pytest.mark.asyncio
@pytest.mark.config
class TestWsEventWaiter:
    """Regression coverage for the #1152 WS-event-driven waiter."""

    async def test_helper_create_emits_no_soft_failure_warning(
        self, mcp_client, cleanup_tracker
    ):
        """A successful create on the test HA must not surface the
        "not yet queryable" warning. That warning was the failure mode
        that motivated #1152 — its presence here would mean the WS waiter
        timed out and we fell back to a stale REST sample."""
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "WS Waiter Regression",
                "min": 0,
                "max": 100,
                "step": 1,
                "initial": 0,
            },
        )

        data = assert_mcp_success(create_result, "Create input_number for #1152")
        entity_id = _entity_id_from_response(data, "input_number")
        assert entity_id, f"Missing entity_id in create response: {data}"
        cleanup_tracker.track("input_number", entity_id)

        warnings = data.get("warnings") or []
        # "not yet queryable" is the exact substring tools_config_helpers
        # emits when the inline wait_for_entity_registered call timed out
        # (search the source for "not yet queryable" to find the emit sites).
        # Its presence here means the WS waiter timed out.
        offending = [w for w in warnings if "not yet queryable" in str(w)]
        assert not offending, (
            f"WS-event waiter regression: helper create surfaced "
            f"soft-failure warnings: {offending} (full response: {data})"
        )

    async def test_multiple_rapid_creates_succeed_under_sequential_load(
        self, mcp_client, cleanup_tracker
    ):
        """Smoke test for the per-call subscribe/unsubscribe path under
        sequential load. Five back-to-back creates exercise the WS waiter
        five times against the shared pooled connection; each must complete
        without a soft-failure warning and the final
        ``wait_for_entity_registration`` probe must find every entity.

        This does NOT directly assert handler/subscription counts on the
        shared WS client (the MCP layer can't observe those) — a true
        leak test lives in the unit-test FakeWebSocketClient suite. Here
        we only verify that whatever cleanup happens is good enough to
        keep N+1th waits working."""
        created: list[str] = []
        for i in range(5):
            result = await mcp_client.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "name": f"WS Waiter Loop {i}",
                },
            )
            data = assert_mcp_success(result, f"Create input_boolean #{i}")
            entity_id = _entity_id_from_response(data, "input_boolean")
            assert entity_id, f"Missing entity_id in response: {data}"
            cleanup_tracker.track("input_boolean", entity_id)
            created.append(entity_id)
            warnings = data.get("warnings") or []
            offending = [w for w in warnings if "not yet queryable" in str(w)]
            assert not offending, (
                f"Soft-failure warning on iteration {i} for {entity_id}: {offending}"
            )

        # Sanity probe: all five should be queryable now via the public
        # registration wait. If any wait silently broke the WS handler
        # state, this lights up.
        for entity_id in created:
            ready = await wait_for_entity_registration(mcp_client, entity_id)
            assert ready, f"Entity {entity_id} not registered after rapid creates"

    async def test_remove_helper_clears_state(self, mcp_client, cleanup_tracker):
        """``ha_config_set_helper`` create + ``ha_delete_helpers_integrations``
        delete should both complete inline without soft-failure warnings,
        validating both the registration and removal WS waiters in one
        round trip."""
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_text",
                "name": "WS Waiter Removal",
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_text")
        entity_id = _entity_id_from_response(create_data, "input_text")
        assert entity_id, f"Missing entity_id in create response: {create_data}"
        cleanup_tracker.track("input_text", entity_id)

        delete_result = await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {
                "helper_type": "input_text",
                "target": entity_id,
                "confirm": True,
            },
        )
        delete_data = assert_mcp_success(delete_result, "Delete input_text")

        warnings = delete_data.get("warnings") or []
        offending = [
            w
            for w in warnings
            if "still exists" in str(w) or "still queryable" in str(w)
        ]
        assert not offending, (
            f"Removal waiter regression: {offending} (full: {delete_data})"
        )
