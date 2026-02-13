"""
E2E tests for the ha_deep_search 3-tier bulk fetch strategy.

Validates that the bulk config fetching logic (REST bulk -> WebSocket bulk ->
time-budgeted individual) works correctly and returns accurate search results
with config data populated.

These tests complement the existing test_deep_search.py tests, which focus on
single-entity search correctness. These tests specifically validate:
  - Bulk fetch populates config data across multiple automations/scripts
  - Config-only matches are found (content not in the name, only in config)
  - Search completes well within the MCP timeout window
  - Results include the config object (proving bulk fetch worked)
"""

import asyncio
import logging
import time
import uuid

import pytest

from ..utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)

# Per-session unique marker so parallel workers never collide
_RUN_ID = uuid.uuid4().hex[:8]
_MARKER = f"bft{_RUN_ID}"

# Number of automations to create — enough to exercise the bulk path
# but not so many that setup/teardown dominates test time.
_AUTOMATION_COUNT = 10
_SCRIPT_COUNT = 5


def _automation_config(index: int) -> dict:
    """Build a test automation config with a unique, searchable token in the action."""
    return {
        "alias": f"{_MARKER} Automation {index}",
        "description": f"Test automation {index} for bulk fetch E2E validation",
        "trigger": [
            {
                "platform": "state",
                "entity_id": f"sensor.{_MARKER}_trigger_{index}",
                "to": "on",
            }
        ],
        "action": [
            {
                "service": "notify.persistent_notification",
                "data": {
                    "message": f"{_MARKER}_payload_{index}",
                    "title": f"Bulk test {index}",
                },
            }
        ],
    }


def _script_config(index: int) -> dict:
    """Build a test script config with a unique, searchable token in the sequence."""
    return {
        "alias": f"{_MARKER} Script {index}",
        "sequence": [
            {
                "service": "notify.persistent_notification",
                "data": {
                    "message": f"{_MARKER}_script_payload_{index}",
                },
            },
            {"delay": {"seconds": 1}},
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def bulk_automations(mcp_client):
    """Create a batch of test automations and tear them down after the test."""
    created_ids = []

    for i in range(_AUTOMATION_COUNT):
        result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {"config": _automation_config(i)},
        )
        data = assert_mcp_success(result, f"Create bulk automation {i}")
        # Use the actual entity_id returned by HA (handles conflicts with _2 suffix)
        entity_id = data.get("entity_id")
        if not entity_id:
            # Fallback to predicted ID if not returned (shouldn't happen)
            entity_id = f"automation.{_MARKER}_automation_{i}"
            logger.warning(f"No entity_id returned for automation {i}, using predicted: {entity_id}")
        created_ids.append(entity_id)
        logger.info(f"Created automation {i}/{_AUTOMATION_COUNT}: {entity_id}")

    # Poll until all entities are registered (more robust than fixed sleep)
    from ..utilities.wait_helpers import wait_for_condition

    async def all_entities_registered():
        states = await mcp_client.call_tool("ha_list_states", {})
        state_data = assert_mcp_success(states, "Get states for polling")
        registered_ids = {s.get("entity_id") for s in state_data.get("states", [])}
        return all(eid in registered_ids for eid in created_ids)

    await wait_for_condition(
        all_entities_registered,
        description=f"All {len(created_ids)} bulk automations registered",
        timeout=10.0,
    )

    yield created_ids

    # Cleanup
    for auto_id in created_ids:
        try:
            await mcp_client.call_tool(
                "ha_config_remove_automation",
                {"identifier": auto_id},
            )
        except Exception as e:
            logger.warning(f"Cleanup failed for {auto_id}: {e}")
    logger.info(f"Cleaned up {len(created_ids)} bulk test automations")


@pytest.fixture
async def bulk_scripts(mcp_client):
    """Create a batch of test scripts and tear them down after the test."""
    created_ids = []

    for i in range(_SCRIPT_COUNT):
        script_id = f"{_MARKER}_script_{i}"
        result = await mcp_client.call_tool(
            "ha_config_set_script",
            {"script_id": script_id, "config": _script_config(i)},
        )
        assert_mcp_success(result, f"Create bulk script {i}")
        created_ids.append(script_id)
        logger.info(f"Created script {i}/{_SCRIPT_COUNT}: {script_id}")

    # Poll until all script entities are registered (more robust than fixed sleep)
    from ..utilities.wait_helpers import wait_for_condition

    async def all_scripts_registered():
        states = await mcp_client.call_tool("ha_list_states", {})
        state_data = assert_mcp_success(states, "Get states for script polling")
        registered_ids = {
            s.get("entity_id") for s in state_data.get("states", [])
            if s.get("entity_id", "").startswith("script.")
        }
        expected_entity_ids = {f"script.{sid}" for sid in created_ids}
        return expected_entity_ids.issubset(registered_ids)

    await wait_for_condition(
        all_scripts_registered,
        description=f"All {len(created_ids)} bulk scripts registered",
        timeout=10.0,
    )

    yield created_ids

    for script_id in created_ids:
        try:
            await mcp_client.call_tool(
                "ha_config_remove_script",
                {"script_id": script_id},
            )
        except Exception as e:
            logger.warning(f"Cleanup failed for script {script_id}: {e}")
    logger.info(f"Cleaned up {len(created_ids)} bulk test scripts")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_bulk_fetch_finds_automation_by_config_content(
    mcp_client, bulk_automations
):
    """
    Verify that deep search with bulk fetch finds automations by content that
    only appears inside their config (trigger entity_id), not in the name.

    This proves the bulk fetch actually populated config data, because a
    name-only search would never match the trigger entity.
    """
    # Search for a trigger entity that only exists inside config
    search_term = f"{_MARKER}_trigger_3"

    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": search_term, "search_types": ["automation"], "limit": 20},
    )
    data = assert_mcp_success(result, "Bulk fetch automation config search")

    automations = data.get("automations", [])
    assert len(automations) > 0, (
        f"Expected to find automation containing '{search_term}' in config. "
        "Bulk fetch may not have populated config data."
    )

    # The specific automation with trigger_3 should be in the results
    matched = [
        a
        for a in automations
        if f"{_MARKER} Automation 3" in a.get("friendly_name", "")
    ]
    assert len(matched) == 1, (
        f"Expected exactly 1 match for automation 3, got {len(matched)}: "
        f"{[a.get('friendly_name') for a in automations]}"
    )

    hit = matched[0]
    assert hit.get("match_in_config") is True, (
        "match_in_config should be True — the search term is only in config"
    )
    assert hit.get("config") is not None, (
        "Config object should be present in the result (proves bulk fetch worked)"
    )
    logger.info(
        f"Found automation 3 via config match, score={hit.get('score')}"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_bulk_fetch_finds_script_by_config_content(
    mcp_client, bulk_scripts
):
    """
    Verify that deep search with bulk fetch finds scripts by content that
    only appears inside their config (the unique payload message).
    """
    search_term = f"{_MARKER}_script_payload_2"

    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": search_term, "search_types": ["script"], "limit": 20},
    )
    data = assert_mcp_success(result, "Bulk fetch script config search")

    scripts = data.get("scripts", [])
    assert len(scripts) > 0, (
        f"Expected to find script containing '{search_term}' in config. "
        "Bulk fetch may not have populated script config data."
    )

    matched = [
        s
        for s in scripts
        if f"{_MARKER} Script 2" in s.get("friendly_name", "")
    ]
    assert len(matched) == 1, (
        f"Expected exactly 1 match for script 2, got {len(matched)}"
    )

    hit = matched[0]
    assert hit.get("match_in_config") is True, (
        "match_in_config should be True for a config-only match"
    )
    assert hit.get("config") is not None, (
        "Config object should be present (proves bulk fetch worked)"
    )
    logger.info(
        f"Found script 2 via config match, score={hit.get('score')}"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_bulk_fetch_populates_config_for_multiple_results(
    mcp_client, bulk_automations
):
    """
    Search for the shared marker token that appears in ALL test automations'
    config. Verify that multiple results are returned and each has its config
    populated — this confirms bulk fetch didn't just fetch one config.
    """
    # The marker appears in every automation's action message and trigger entity
    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": f"{_MARKER}_payload", "search_types": ["automation"], "limit": 20},
    )
    data = assert_mcp_success(result, "Bulk fetch multi-result search")

    automations = data.get("automations", [])

    # We should find multiple automations (all share the marker in config)
    bulk_matches = [
        a
        for a in automations
        if f"{_MARKER} Automation" in a.get("friendly_name", "")
    ]
    assert len(bulk_matches) >= 3, (
        f"Expected at least 3 bulk test automations in results, got {len(bulk_matches)}. "
        f"Total results: {len(automations)}"
    )

    # Every matched automation should have config populated
    configs_present = sum(1 for a in bulk_matches if a.get("config") is not None)
    assert configs_present == len(bulk_matches), (
        f"All {len(bulk_matches)} matched automations should have config populated, "
        f"but only {configs_present} do. Bulk fetch may have partially failed."
    )

    logger.info(
        f"Found {len(bulk_matches)} automations with configs populated "
        f"(out of {_AUTOMATION_COUNT} created)"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_bulk_fetch_completes_within_timeout(
    mcp_client, bulk_automations, bulk_scripts
):
    """
    Verify that deep search across all types completes well within the
    30-second MCP timeout, even with multiple automations and scripts.

    This is not a strict performance test — it validates the bulk fetch
    strategy avoids the sequential-fetch timeout that prompted this PR.
    """
    start = time.perf_counter()

    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": _MARKER, "limit": 50},
    )
    data = assert_mcp_success(result, "Bulk fetch timing validation")

    elapsed = time.perf_counter() - start

    # The whole search should complete well under the 30s MCP timeout.
    # With bulk fetch working, this typically takes < 5 seconds.
    assert elapsed < 25.0, (
        f"Deep search took {elapsed:.1f}s — dangerously close to or exceeding "
        f"the 30s MCP timeout. Bulk fetch may not be working."
    )

    total = data.get("total_matches", 0)
    logger.info(
        f"Deep search completed in {elapsed:.1f}s, found {total} matches "
        f"across {_AUTOMATION_COUNT} automations + {_SCRIPT_COUNT} scripts"
    )


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_bulk_fetch_result_structure_integrity(
    mcp_client, bulk_automations
):
    """
    Verify that results from bulk-fetched configs have the correct structure:
    entity_id, friendly_name, score, match flags, and config with expected keys.
    """
    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": f"{_MARKER}_trigger", "search_types": ["automation"], "limit": 20},
    )
    data = assert_mcp_success(result, "Bulk fetch structure check")

    automations = data.get("automations", [])
    assert len(automations) > 0, "Should find at least one automation"

    for auto in automations:
        if f"{_MARKER} Automation" not in auto.get("friendly_name", ""):
            continue

        # Required top-level fields
        assert "entity_id" in auto, "Missing entity_id"
        assert auto["entity_id"].startswith("automation."), "Bad entity_id format"
        assert "friendly_name" in auto, "Missing friendly_name"
        assert isinstance(auto.get("score"), (int, float)), "Score should be numeric"
        assert isinstance(auto.get("match_in_name"), bool), "match_in_name should be bool"
        assert isinstance(auto.get("match_in_config"), bool), "match_in_config should be bool"

        # Config should be a dict with automation keys
        config = auto.get("config")
        if config is not None:
            assert isinstance(config, dict), "Config should be a dict"
            # Bulk-fetched configs should contain the automation structure
            has_trigger = "trigger" in config or "triggers" in config
            has_action = "action" in config or "actions" in config
            assert has_trigger or has_action or "alias" in config, (
                f"Config should contain automation keys, got: {list(config.keys())}"
            )

    logger.info(f"Structure validation passed for {len(automations)} results")
