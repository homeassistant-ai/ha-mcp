"""E2E: entity visibility filter end-to-end through real MCP dispatch + real HA.

Covers the soft-only, opt-in visibility filter (#1728) at the outermost seam a
user actually hits: the ``ha_search`` MCP tool dispatched against a live Home
Assistant, with a real entity registered in the registry.

CONFIG-DIR SEAM NOTE
--------------------
Authored against the *container* / *haos-external* e2e backends, where the MCP
server runs IN-PROCESS in the pytest host (see the ``mcp_server`` fixture in
``conftest.py`` — ``HomeAssistantSmartMCPServer(client=client)`` + in-memory
transport). Because the server shares this process, the config-dir seam
(``resolver.get_data_dir``) can be redirected with ``monkeypatch`` — no
container bind-mount staging is required (the config is read by the host
process, not inside the HA container). The filter *logic* is additionally
covered by runnable in-process unit tests in ``tests/src/unit/visibility/``
that do not need Docker.

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
        # ha_get_state returns a {data, metadata} envelope with no top-level
        # `success` key, so assert success via the shared helper (which treats
        # "has data, no error" as success) rather than a raw `success` check.
        got_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": _PROBE_ENTITY_ID}
        )
        assert_mcp_success(got_result, "targeted read still returns hidden entity")
        got = parse_mcp_result(got_result)
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


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_label_dimension_hides_entity_from_search(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """A registry-derived dimension (exclude_labels) end-to-end: a label assigned
    to the probe helper hides it from ha_search. Exercises the registry-join path
    (the resolver reading entry["labels"]), not just the registry-independent
    denylist the sibling test covers."""
    probe_name = "Zzvis Label Probe E2E"
    probe_entity_id = "input_boolean.zzvis_label_probe_e2e"
    probe_query = "zzvis_label_probe_e2e"
    probe_target = "zzvis_label_probe_e2e"
    label_id = None

    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": probe_name},
    )
    assert_mcp_success(create_result, "Create label-probe helper")

    try:
        # Create a label and assign it to the probe so the entity-registry entry
        # carries it (the resolver matches config.exclude_labels against
        # entry["labels"]).
        label_result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Zzvis E2E Hidden Label"}
        )
        label_data = assert_mcp_success(label_result, "Create probe label")
        label_id = label_data.get("label_id")
        assert label_id, f"label_id missing from create response: {label_data}"

        assign_result = await mcp_client.call_tool(
            "ha_set_entity", {"entity_id": probe_entity_id, "labels": [label_id]}
        )
        assert_mcp_success(assign_result, "Assign label to probe helper")

        # Baseline (filter OFF): probe is searchable. The wait also lets the
        # registry label write propagate before the filter reads it.
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": probe_query, "limit": 10},
            predicate=lambda d: probe_entity_id in _entity_ids(d),
            description="baseline search finds labelled visibility probe",
        )

        # Act: enable the filter excluding that label (registry-derived dimension).
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                exclude_labels=[label_id],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        # Assert: search now omits it via the label match. Unique query => the
        # only match was excluded => post-filter total is 0 (filter is
        # pre-pagination, so the count stays coherent).
        filtered = parse_mcp_result(
            await mcp_client.call_tool("ha_search", {"query": probe_query, "limit": 10})
        )
        assert filtered.get("success") is True
        assert probe_entity_id not in _entity_ids(filtered)
        assert filtered.get("entity_total_matches") == 0

    finally:
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": probe_target,
                "confirm": True,
            },
        )
        if label_id:
            await mcp_client.call_tool("ha_config_remove_label", {"label_id": label_id})
