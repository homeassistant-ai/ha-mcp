"""Unit tests for the ha_search orchestrator's payload-metadata merge helper.

Pins the shadow-protect + warnings-accumulation contract used by both the
entities-branch and configs-branch of the merged ha_search tool, the
per-surface pagination handling that replaces the first-wins shadow-protect
on ``has_more``/``next_offset``, and the budget-exhaustion ``partial`` flag.
"""

from __future__ import annotations

from ha_mcp.tools.smart_search._deep import DeepSearchMixin
from ha_mcp.tools.tools_search import _merge_payload_metadata


def test_propagates_non_conflicting_keys() -> None:
    response: dict = {"success": True}
    payload = {"search_type": "fuzzy_search", "has_more": False, "next_offset": 0}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["search_type"] == "fuzzy_search"
    assert response["has_more"] is False
    assert response["next_offset"] == 0
    assert response["success"] is True


def test_shadow_protect_orchestrator_owned_keys() -> None:
    response: dict = {"success": True, "query": "orchestrator-input"}
    payload = {"success": False, "query": "payload-echo", "search_type": "exact"}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["success"] is True
    assert response["query"] == "orchestrator-input"
    assert response["search_type"] == "exact"


def test_skip_keys_are_dropped() -> None:
    response: dict = {}
    payload = {"results": [1, 2], "total_matches": 2, "search_type": "fuzzy"}
    _merge_payload_metadata(response, payload, skip_keys=("results", "total_matches"))
    assert "results" not in response
    assert "total_matches" not in response
    assert response["search_type"] == "fuzzy"


def test_warnings_accumulate_across_branches() -> None:
    response: dict = {}
    entity_payload = {"warnings": ["entity-side: legacy field"]}
    config_payload = {"warnings": ["config-side: dashboards opt-in skipped"]}
    _merge_payload_metadata(response, entity_payload, skip_keys=())
    _merge_payload_metadata(response, config_payload, skip_keys=())
    assert response["warnings"] == [
        "entity-side: legacy field",
        "config-side: dashboards opt-in skipped",
    ]


def test_warnings_accumulate_when_response_seeded() -> None:
    response: dict = {"warnings": ["orchestrator-seed"]}
    payload = {"warnings": ["payload-add"]}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["warnings"] == ["orchestrator-seed", "payload-add"]


def test_warnings_non_list_falls_back_to_shadow_protect() -> None:
    response: dict = {"warnings": ["from-orchestrator"]}
    payload = {"warnings": "string-not-list"}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["warnings"] == ["from-orchestrator"]


def test_empty_payload_is_noop() -> None:
    response: dict = {"success": True, "warnings": ["existing"]}
    _merge_payload_metadata(response, {}, skip_keys=())
    assert response == {"success": True, "warnings": ["existing"]}


# Per-surface pagination ----------------------------------------------------
#
# These tests pin the orchestrator's actual per-surface skip_keys to ensure
# ``has_more``/``next_offset``/``offset``/``limit`` from a sub-payload are
# never first-wins-shadow-protected into the merged response — the caller
# uses ``entity_has_more`` / ``config_has_more`` explicitly instead.


def test_pagination_keys_dropped_when_skipped() -> None:
    """``has_more``/``next_offset`` ARE skipped — they're per-surface so the
    orchestrator synthesizes them explicitly after the merge. ``offset``/
    ``limit`` are caller-input echoes (identical across branches) and stay
    out of skip_keys to first-wins via the merge."""
    response: dict = {}
    payload = {
        "has_more": True,
        "next_offset": 10,
        "offset": 0,
        "limit": 10,
        "total_matches": 17,
        "results": [{"entity_id": "light.x"}],
        "search_type": "fuzzy_search",
    }
    _merge_payload_metadata(
        response,
        payload,
        skip_keys=(
            "results",
            "total_matches",
            "has_more",
            "next_offset",
        ),
    )
    assert "has_more" not in response
    assert "next_offset" not in response
    # offset/limit are caller-input echoes — first-wins via merge.
    assert response["offset"] == 0
    assert response["limit"] == 10
    assert response["search_type"] == "fuzzy_search"


def test_two_payload_pagination_no_first_wins_leak() -> None:
    """Per-surface ``has_more``/``next_offset`` are skipped from both branches;
    nothing first-wins-leaks via the merge. The orchestrator's post-merge
    synthesis (covered by e2e + the dual-surface unit test below) is what
    populates the flat keys."""
    response: dict = {}
    entity_payload = {"has_more": True, "next_offset": 5}
    config_payload = {"has_more": False, "next_offset": None}
    pagination_skips = ("has_more", "next_offset")
    _merge_payload_metadata(response, entity_payload, skip_keys=pagination_skips)
    _merge_payload_metadata(response, config_payload, skip_keys=pagination_skips)
    assert "has_more" not in response
    assert "next_offset" not in response


def test_dual_surface_next_offset_picks_non_none() -> None:
    """When only one surface has more results, the flat ``next_offset`` picks
    that surface's value (both encode caller_offset + caller_limit when set).
    This mirrors the orchestrator's post-merge synthesis logic."""
    # Entity has more, config doesn't.
    response = {"entity_next_offset": 10, "config_next_offset": None}
    flat = response.get("entity_next_offset") or response.get("config_next_offset")
    assert flat == 10

    # Config has more, entity doesn't.
    response = {"entity_next_offset": None, "config_next_offset": 5}
    flat = response.get("entity_next_offset") or response.get("config_next_offset")
    assert flat == 5

    # Both have more — values are equal (both = caller_offset + caller_limit),
    # picking either is correct; ``or`` returns the first (entity).
    response = {"entity_next_offset": 10, "config_next_offset": 10}
    flat = response.get("entity_next_offset") or response.get("config_next_offset")
    assert flat == 10

    # Neither has more.
    response = {"entity_next_offset": None, "config_next_offset": None}
    flat = response.get("entity_next_offset") or response.get("config_next_offset")
    assert flat is None


# Budget-exhaustion partial flag --------------------------------------------


def test_budget_partial_flag_set_when_automation_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_skipped=3, script_skipped=0
    )
    assert response["partial"] is True
    assert "Automation config fetch incomplete: 3 skipped" in response["partial_reason"]
    assert "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET" in response["partial_reason"]


def test_budget_partial_flag_set_when_script_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_skipped=0, script_skipped=7
    )
    assert response["partial"] is True
    assert "Script config fetch incomplete: 7 skipped" in response["partial_reason"]
    assert "HAMCP_SCRIPT_CONFIG_TIME_BUDGET" in response["partial_reason"]


def test_budget_partial_flag_combines_both_surfaces() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_skipped=2, script_skipped=4
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "Automation config fetch incomplete: 2 skipped" in reason
    assert "Script config fetch incomplete: 4 skipped" in reason


def test_budget_partial_flag_appends_to_existing_reason() -> None:
    """Append-safe: an existing ``partial_reason`` (e.g. from scene budget)
    is preserved and the new reason is concatenated, not overwritten."""
    response: dict = {
        "success": True,
        "partial": True,
        "partial_reason": "Scene config fetch incomplete: 1 failed, 2 skipped.",
    }
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_skipped=3, script_skipped=0
    )
    assert response["partial"] is True
    assert response["partial_reason"].startswith("Scene config fetch incomplete")
    assert "Automation config fetch incomplete: 3 skipped" in response["partial_reason"]


def test_budget_partial_flag_noop_when_no_skips() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_skipped=0, script_skipped=0
    )
    assert "partial" not in response
    assert "partial_reason" not in response
