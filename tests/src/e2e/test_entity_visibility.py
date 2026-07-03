"""E2E: entity visibility filter end-to-end through real MCP dispatch + real HA.

Covers the soft-only, opt-in visibility filter (#1728) at the outermost seam a
user actually hits: the ``ha_search`` MCP tool dispatched against a live Home
Assistant, with a real entity registered in the registry.

LOCAL VERIFICATION NOTE
-----------------------
Authored against the *container* / *haos-external* e2e backends, where the MCP
server runs IN-PROCESS in the pytest host (see the ``mcp_server`` fixture in
``conftest.py`` — ``HomeAssistantSmartMCPServer(client=client)`` + in-memory
transport). Because the server shares this process, the config-dir seam
(``resolver.get_data_dir``) can be redirected with ``monkeypatch`` — no
container bind-mount staging is required (the config is read by the host
process, not inside the HA container).

This file was NOT executed locally in the authoring environment (no Docker
available there); CI is the verification gate for it. The filter *logic* is
additionally covered by runnable in-process tests in
``tests/src/unit/visibility/`` that do not need Docker.

The ``external_only`` marker skips the HAOS-inaddon backend, where the server
runs in a *separate* addon container and in-process ``monkeypatch`` cannot
reach it (see the marker rationale in ``pyproject.toml``). The component path
is backend-identical, so the container run covers it.
"""

import pytest

from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from .utilities.assertions import assert_mcp_success, parse_mcp_result
from .utilities.wait_helpers import wait_for_tool_result

# Unique so the exact-match query resolves to exactly this one entity — that
# makes the post-filter coherence assertion (entity_total_matches == 0) exact.
_PROBE_NAME = "Zzvis Probe Unique E2E"
_PROBE_ENTITY_ID = "input_boolean.zzvis_probe_unique_e2e"
_PROBE_QUERY = "zzvis_probe_unique_e2e"


def _entity_ids(search_data: dict) -> list[str]:
    """Extract entity_ids from an ``ha_search`` response ``entities`` bucket."""
    return [e.get("entity_id") for e in search_data.get("entities", [])]


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_denylist_hides_entity_from_search_but_get_state_returns_it(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """Enabled denylist removes the entity from ha_search yet ha_get_state
    (a targeted read) still returns it — the Tier-B contract."""
    # Arrange: create a helper entity in the live registry.
    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": _PROBE_NAME},
    )
    assert_mcp_success(create_result, "Create visibility probe helper")

    try:
        # Baseline (filter OFF): prove the entity IS searchable before filtering,
        # so the later absence assertion is meaningful (guards the "entity not
        # registered yet" false-pass trap, and validates the derived slug).
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": _PROBE_QUERY, "limit": 10},
            predicate=lambda d: _PROBE_ENTITY_ID in _entity_ids(d),
            description="baseline search finds visibility probe",
        )

        # Act: enable the filter with the probe on the denylist. Seeded to a
        # tmp data-dir that the in-process server's resolver is redirected to.
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                deny_entity_ids=[_PROBE_ENTITY_ID],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        # Assert: search now omits it. Registration already confirmed above, so
        # a single call suffices (no wait-for-absence, which could false-pass).
        filtered = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_search", {"query": _PROBE_QUERY, "limit": 10}
            )
        )
        assert filtered.get("success") is True
        assert _PROBE_ENTITY_ID not in _entity_ids(filtered)
        # Coherence: the filter sits pre-pagination, so the total reflects the
        # post-filter set. Unique query => the only match was excluded => 0.
        assert filtered.get("entity_total_matches") == 0

        # Tier B: a targeted read is NOT filtered — it still returns the entity.
        got = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_get_state", {"entity_id": _PROBE_ENTITY_ID}
            )
        )
        assert got.get("success") is True
        assert got.get("data", {}).get("entity_id") == _PROBE_ENTITY_ID

    finally:
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": "zzvis_probe_unique_e2e",
                "confirm": True,
            },
        )
