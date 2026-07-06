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


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_allowlist_hides_unlisted_entity_from_search(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """An active allowlist inverts the filter: an entity NOT on the allowlist is
    hidden from ha_search even though no exclude/deny dimension targets it.
    Exercises the restrict-mode branch end-to-end through real MCP dispatch."""
    probe_name = "Zzvis Allow Probe E2E"
    probe_entity_id = "input_boolean.zzvis_allow_probe_e2e"
    probe_query = "zzvis_allow_probe_e2e"
    probe_target = "zzvis_allow_probe_e2e"

    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": probe_name},
    )
    assert_mcp_success(create_result, "Create allowlist probe helper")

    try:
        # Baseline (filter OFF): probe is searchable.
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": probe_query, "limit": 10},
            predicate=lambda d: probe_entity_id in _entity_ids(d),
            description="baseline search finds allowlist probe",
        )

        # Act: enable an allowlist that does NOT include the probe. Restrict mode
        # then hides everything unmatched, including this probe.
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                allow_entity_ids=["input_boolean.some_other_allowed_entity"],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

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


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_allowlist_keeps_allowed_visible_and_deny_wins(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """The allowlist positive side + precedence the one-sided sibling misses: an
    allowlisted entity stays visible while an unlisted one is hidden, and deny
    wins over an allow match (so a restrict-mode-hides-everything regression could
    not pass this)."""
    allowed_id = "input_boolean.zzvis_allowlisted_e2e"
    allowed_query = "zzvis_allowlisted_e2e"
    unlisted_id = "input_boolean.zzvis_unlisted_e2e"
    unlisted_query = "zzvis_unlisted_e2e"

    r1 = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": "Zzvis Allowlisted E2E"},
    )
    assert_mcp_success(r1, "Create allowlisted probe")
    r2 = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": "Zzvis Unlisted E2E"},
    )
    assert_mcp_success(r2, "Create unlisted probe")

    try:
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": allowed_query, "limit": 10},
            predicate=lambda d: allowed_id in _entity_ids(d),
            description="baseline search finds allowlisted probe",
        )

        # Allowlist contains only the allowed probe -> restrict mode.
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                allow_entity_ids=[allowed_id],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        # Positive side: the allowlisted entity stays visible.
        allowed_res = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_search", {"query": allowed_query, "limit": 10}
            )
        )
        assert allowed_res.get("success") is True
        assert allowed_id in _entity_ids(allowed_res)

        # Negative side: an unlisted entity is hidden by the restriction.
        unlisted_res = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_search", {"query": unlisted_query, "limit": 10}
            )
        )
        assert unlisted_id not in _entity_ids(unlisted_res)
        assert unlisted_res.get("entity_total_matches") == 0

        # Deny wins over allow: denying the allowlisted entity hides it despite the
        # allow match.
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                allow_entity_ids=[allowed_id],
                deny_entity_ids=[allowed_id],
            ),
        )
        denied_res = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_search", {"query": allowed_query, "limit": 10}
            )
        )
        assert allowed_id not in _entity_ids(denied_res)
        assert denied_res.get("entity_total_matches") == 0

    finally:
        for target in (allowed_query, unlisted_query):
            await mcp_client.call_tool(
                "ha_remove_helpers_integrations",
                {"helper_type": "input_boolean", "target": target, "confirm": True},
            )


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_filter_applies_to_get_overview(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """ha_get_overview — the second filtered collection tool, previously with no
    e2e — honors the filter: a denied probe drops out of the overview's entity
    total (the filter sits before the counts, so the total stays coherent)."""
    probe_id = "input_boolean.zzvis_overview_probe_e2e"
    probe_query = "zzvis_overview_probe_e2e"

    create = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": "Zzvis Overview Probe E2E"},
    )
    assert_mcp_success(create, "Create overview probe")

    try:
        # Ensure the probe is registered (also the filter-OFF baseline).
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": probe_query, "limit": 10},
            predicate=lambda d: probe_id in _entity_ids(d),
            description="baseline search finds overview probe",
        )
        baseline = parse_mcp_result(
            await mcp_client.call_tool("ha_get_overview", {"detail_level": "full"})
        )
        baseline_total = baseline["system_summary"]["total_entities"]

        # Enable the filter denying the probe.
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                deny_entity_ids=[probe_id],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        filtered = parse_mcp_result(
            await mcp_client.call_tool("ha_get_overview", {"detail_level": "full"})
        )
        assert filtered.get("success") is True
        # Exactly the denied probe left the counted universe.
        assert filtered["system_summary"]["total_entities"] == baseline_total - 1

    finally:
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {"helper_type": "input_boolean", "target": probe_query, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.external_only
async def test_visibility_respect_assist_honors_explicit_unexpose(
    mcp_client, ha_container_with_fresh_config, tmp_path, monkeypatch
):
    """respect_assist_exposure reads the explicit per-entity ``should_expose`` from
    the registry entry ``options`` (carried by ``config/entity_registry/list``), so
    an explicitly un-exposed entity is hidden — the regression for the round-2
    Assist finding, where exposure was read from expose_entity/list (True-only) and
    an explicit un-expose was invisible. The explicit expose->unexpose transition
    keeps the assertion independent of domain defaults (input_boolean is not
    default-exposed, so the visible baseline can only come from the explicit True
    override)."""
    probe_id = "input_boolean.zzvis_assist_probe_e2e"
    probe_query = "zzvis_assist_probe_e2e"

    create = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": "Zzvis Assist Probe E2E"},
    )
    assert_mcp_success(create, "Create assist probe")

    try:
        # Explicitly expose to conversation: options.conversation.should_expose=True.
        expose = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": probe_id, "expose_to": {"conversation": True}},
        )
        assert_mcp_success(expose, "Expose probe to conversation")

        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                respect_assist_exposure=True,
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        # Explicitly exposed -> stays visible (proves the override is read; an
        # input_boolean is not default-exposed, so nothing else would show it).
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": probe_query, "limit": 10},
            predicate=lambda d: probe_id in _entity_ids(d),
            description="explicitly exposed probe stays visible under respect_assist",
        )

        # Explicitly un-expose: options.conversation.should_expose=False. The
        # round-2 bug could not read this False; the fix reads it from the registry
        # entry options in the entity_registry/list payload.
        unexpose = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": probe_id, "expose_to": {"conversation": False}},
        )
        assert_mcp_success(unexpose, "Un-expose probe from conversation")

        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={"query": probe_query, "limit": 10},
            predicate=lambda d: probe_id not in _entity_ids(d),
            description="explicitly un-exposed probe is hidden under respect_assist",
        )

    finally:
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {"helper_type": "input_boolean", "target": probe_query, "confirm": True},
        )
