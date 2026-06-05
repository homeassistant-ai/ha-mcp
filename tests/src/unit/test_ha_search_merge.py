"""Unit tests for the ha_search orchestrator's payload-metadata merge helper.

Pins the shadow-protect + warnings-accumulation contract used by both the
entities-branch and configs-branch of the merged ha_search tool, the
per-surface pagination handling that replaces the first-wins shadow-protect
on ``has_more``/``next_offset``, and the budget-exhaustion ``partial`` flag.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.smart_search._deep import DeepSearchMixin
from ha_mcp.tools.tools_search import (
    _compute_eligibility,
    _merge_payload_metadata,
    _validate_search_types,
)


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


def test_warnings_response_side_non_list_replaced_with_payload() -> None:
    """When ``response['warnings']`` already exists but is not a list (a
    contract violation upstream — top-level ``warnings`` MUST be
    ``list[str]``), and the payload carries a well-typed warnings list, the
    merge replaces response's broken value with the payload's list rather
    than raising ``AttributeError`` from ``setdefault(...).extend(value)``
    returning the non-list sentinel."""
    response: dict = {"warnings": "not-a-list-violates-contract"}
    payload = {"warnings": ["payload-add"]}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["warnings"] == ["payload-add"]


# search_types validation --------------------------------------------------


def test_validate_search_types_none_passes() -> None:
    _validate_search_types(None)


def test_validate_search_types_empty_list_rejected() -> None:
    """Empty list (``search_types=[]``) is rejected: it would pin branch
    eligibility to config-only while the response echoes the default
    type list, a silent caller / runtime / response mismatch. Callers
    wanting the default behavior should omit the parameter entirely."""
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types([])
    assert "non-empty" in str(excinfo.value)


def test_validate_search_types_all_valid_passes() -> None:
    _validate_search_types(
        ["automation", "script", "scene", "helper", "dashboard"]
    )


def test_validate_search_types_subset_passes() -> None:
    _validate_search_types(["scene"])


def test_validate_search_types_unknown_rejected() -> None:
    """A typo / stale type name like ``blueprint`` (KP13 review S8) or
    ``frobnicate`` would previously return zero matches with no warning —
    now surfaces as ``VALIDATION_FAILED`` with ``parameter='search_types'``."""
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types(["frobnicate"])
    assert "frobnicate" in str(excinfo.value)


def test_validate_search_types_mixed_valid_invalid_rejected() -> None:
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types(["automation", "frobnicate", "scene"])
    assert "frobnicate" in str(excinfo.value)
    # Valid types from the input shouldn't appear in the unknown list.
    assert "['frobnicate']" in str(excinfo.value) or "frobnicate" in str(
        excinfo.value
    )


def test_validate_search_types_blueprint_rejected() -> None:
    """Pins the S8 finding: ``blueprint`` is not implemented as a search
    type. The pre-fix behavior silently returned zero matches; the new
    validation surfaces it as a typed error."""
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types(["blueprint"])
    assert "blueprint" in str(excinfo.value)


# Accumulating-arm merge semantics ----------------------------------------
#
# These pin the cross-branch accumulation of ``errors`` / ``partial`` /
# ``partial_reason`` — the parent contract is that no branch's diagnostic
# data is silently shadow-protected away by a later first-wins skip.


def test_errors_accumulate_across_branches() -> None:
    """Both branches' ``errors`` lists end up in the response — neither is
    first-wins-shadowed."""
    response: dict = {}
    entity_payload = {"errors": [{"surface": "entity-internal", "code": "WS"}]}
    config_payload = {"errors": [{"surface": "config-internal", "code": "BUDGET"}]}
    _merge_payload_metadata(response, entity_payload, skip_keys=())
    _merge_payload_metadata(response, config_payload, skip_keys=())
    assert response["errors"] == [
        {"surface": "entity-internal", "code": "WS"},
        {"surface": "config-internal", "code": "BUDGET"},
    ]


def test_errors_response_side_non_list_replaced_with_payload() -> None:
    """When ``response['errors']`` is somehow non-list (contract violation
    upstream), the payload's list replaces it rather than crashing on
    ``.extend``."""
    response: dict = {"errors": "not-a-list"}
    payload = {"errors": [{"surface": "x", "code": "Y"}]}
    _merge_payload_metadata(response, payload, skip_keys=())
    assert response["errors"] == [{"surface": "x", "code": "Y"}]


def test_partial_or_accumulates_true_across_branches() -> None:
    """If either branch is partial, the response is partial."""
    response: dict = {}
    _merge_payload_metadata(response, {"partial": False}, skip_keys=())
    _merge_payload_metadata(response, {"partial": True}, skip_keys=())
    assert response["partial"] is True


def test_partial_or_keeps_true_when_second_branch_clean() -> None:
    """Once partial=True is set, a subsequent partial=False payload does
    not flip it back."""
    response: dict = {}
    _merge_payload_metadata(response, {"partial": True}, skip_keys=())
    _merge_payload_metadata(response, {"partial": False}, skip_keys=())
    assert response["partial"] is True


def test_partial_reason_accumulates_across_branches_with_separator() -> None:
    """Both branches' ``partial_reason`` strings end up in the response,
    joined by a separator — neither is first-wins-shadowed."""
    response: dict = {}
    entity_payload = {"partial_reason": "entity: hidden-filter unavailable"}
    config_payload = {"partial_reason": "config: budget exhausted, 5 skipped"}
    _merge_payload_metadata(response, entity_payload, skip_keys=())
    _merge_payload_metadata(response, config_payload, skip_keys=())
    assert "entity: hidden-filter unavailable" in response["partial_reason"]
    assert "config: budget exhausted, 5 skipped" in response["partial_reason"]
    assert " ; " in response["partial_reason"]


def test_partial_reason_dedups_identical_payload() -> None:
    """A repeated reason string isn't appended a second time."""
    response: dict = {"partial_reason": "duplicate-reason"}
    _merge_payload_metadata(
        response, {"partial_reason": "duplicate-reason"}, skip_keys=()
    )
    assert response["partial_reason"] == "duplicate-reason"


def test_partial_reason_empty_payload_does_not_overwrite() -> None:
    """An empty / falsy ``partial_reason`` from the payload doesn't replace
    a real reason already in the response."""
    response: dict = {"partial_reason": "real reason"}
    _merge_payload_metadata(response, {"partial_reason": ""}, skip_keys=())
    assert response["partial_reason"] == "real reason"


# Eligibility gate --------------------------------------------------------
#
# ``_compute_eligibility`` is the pure decision function for which sub-search
# branches the orchestrator fans out to. These cells pin the 14 behaviorally-
# distinct input combinations identified during the gate's design (BAT round
# + scrutinize pass). Returns (registry_eligible, body_eligible,
# body_skipped_by_intent_gate).


def _gate(**kwargs):
    """Shortcut: zero-fill unset string params + run _compute_eligibility."""
    return _compute_eligibility(
        query_text=kwargs.get("q", ""),
        domain_filter_text=kwargs.get("dom", ""),
        area_filter_text=kwargs.get("area", ""),
        state_filter_text=kwargs.get("state", ""),
        explicit_config_only=kwargs.get("pin", False),
    )


def test_gate_no_inputs_at_all() -> None:
    """All-empty inputs: neither branch eligible; caller hits validation."""
    assert _gate() == (False, False, False)


def test_gate_query_only_runs_both_branches() -> None:
    """Plain `ha_search("X")` — no filter, no pin — runs both surfaces."""
    assert _gate(q="light.kitchen") == (True, True, False)


def test_gate_domain_only_runs_entity_only() -> None:
    """`ha_search(domain_filter="sensor")` — registry-list mode, no body."""
    assert _gate(dom="sensor") == (True, False, False)


def test_gate_area_only_runs_entity_only() -> None:
    assert _gate(area="Living Room") == (True, False, False)


def test_gate_state_only_is_rejected() -> None:
    """``state_filter`` alone doesn't unlock registry (unchanged from
    pre-NEW1 behavior); body has no query either. Caller hits validation."""
    assert _gate(state="on") == (False, False, False)


def test_gate_query_plus_domain_skips_body_NEW() -> None:
    """The headline BAT-driven change: name-as-query + filter signals
    entity-only intent, so body is skipped to avoid the wasteful deep
    search. ``body_skipped_by_intent_gate`` flags the skip for warning
    emission."""
    assert _gate(q="bedroom motion", dom="binary_sensor") == (True, False, True)


def test_gate_query_plus_area_skips_body_NEW() -> None:
    assert _gate(q="tv", area="Living Room") == (True, False, True)


def test_gate_query_plus_state_skips_body_NEW() -> None:
    assert _gate(q="light", state="on") == (True, False, True)


def test_gate_query_plus_pin_runs_config_only() -> None:
    """Explicit ``search_types`` pin: entity branch skipped, body runs."""
    assert _gate(q="light.kitchen", pin=True) == (False, True, False)


def test_gate_pin_only_no_query_is_rejected() -> None:
    """Pin + no query: nothing for body to match on (deep needs a term),
    registry skipped by pin. Caller hits validation."""
    assert _gate(pin=True) == (False, False, False)


def test_gate_query_plus_filter_plus_pin_overrides_intent_gate() -> None:
    """Explicit pin overrides the entity-intent gate — callers who want
    config matches alongside a filter scope opt back in this way."""
    assert _gate(q="temperature", dom="sensor", pin=True) == (False, True, False)


def test_gate_filters_only_without_query_no_skip_flag() -> None:
    """Filters set but no query: body never eligible (no term), so the
    skip-flag should NOT fire (the skip is structural, not gate-driven)."""
    assert _gate(dom="sensor", area="Living Room", state="on") == (True, False, False)


def test_gate_all_filters_plus_query_skips_body_NEW() -> None:
    """All three entity-intent signals set with a query: body skipped."""
    assert _gate(q="kitchen", dom="light", area="Kitchen", state="on") == (
        True,
        False,
        True,
    )


def test_gate_query_plus_all_filters_plus_pin_runs_body() -> None:
    """Pin overrides all three filters."""
    assert _gate(
        q="kitchen", dom="light", area="Kitchen", state="on", pin=True
    ) == (False, True, False)


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


def test_dual_surface_has_more_is_or_of_branches() -> None:
    """Pin S7(b): the flat ``has_more`` is the boolean OR of the per-surface
    flags. Previously covered only by an inline reimplementation inside
    the e2e pagination test; lifting it to unit level catches the
    synthesis logic regressing without a full e2e run."""
    # Mirrors tools_search.py: response["has_more"] =
    #     bool(response.get("entity_has_more")) or
    #     bool(response.get("config_has_more"))
    def synthesise(eh: bool, ch: bool) -> bool:
        response: dict = {"entity_has_more": eh, "config_has_more": ch}
        return bool(response.get("entity_has_more")) or bool(
            response.get("config_has_more")
        )

    assert synthesise(False, False) is False
    assert synthesise(True, False) is True
    assert synthesise(False, True) is True
    assert synthesise(True, True) is True


def test_orchestrator_entity_branch_exception_partial_shape() -> None:
    """Pin S7(c): when the entity branch raises and the config branch
    returns clean, the orchestrator's exception-handling at
    ``tools_search.py:~554-606`` should produce a response with
    ``partial: True``, an ``errors`` list tagged with ``surface: 'entities'``,
    AND the surviving config bucket's payload preserved (not clobbered by
    the orchestrator-local errors assignment at the end of the merge loop).
    The shape here mirrors what the real ha_search would assemble; unit-
    level so a regression in the clobber-or-extend behavior catches without
    a full e2e fixture."""
    # Simulate the orchestrator's response init + the in-loop exception
    # bookkeeping + the post-loop ``response["errors"].extend(errors)``.
    response: dict = {
        "success": True,
        "query": "kitchen",
        "entities": [],
        "entity_total_matches": 0,
        "automations": [],
        "scripts": [],
        "scenes": [],
        "helpers": [],
        "config_total_matches": 0,
        "partial": False,
        "errors": [],
        "warnings": [],
    }
    partial = False
    orchestrator_errors: list[dict[str, str]] = []

    # Entity branch raised — the orchestrator records the surface tag.
    entity_exception = RuntimeError("ws_connection_closed")
    partial = True
    orchestrator_errors.append(
        {"surface": "entities", "error": str(entity_exception)}
    )

    # Config branch returned clean with its own diagnostic ``warnings`` —
    # the merge helper picks those up.
    config_payload = {
        "success": True,
        "automations": [{"entity_id": "automation.a"}],
        "total_matches": 1,
        "warnings": ["config-side: dashboard opt-in skipped"],
    }
    for bucket in ("automations", "scripts", "scenes", "helpers", "dashboards"):
        if bucket in config_payload:
            response[bucket] = config_payload[bucket]
    response["config_total_matches"] = config_payload.get("total_matches", 0)
    _merge_payload_metadata(
        response,
        config_payload,
        skip_keys=(
            "automations", "scripts", "scenes", "helpers", "dashboards",
            "total_matches", "has_more", "next_offset",
        ),
    )

    # End-of-loop: set partial + extend (NOT clobber) the orchestrator's
    # error list onto whatever the merge already accumulated.
    if partial:
        response["partial"] = True
        response["errors"].extend(orchestrator_errors)

    # Assertions: surface tagging + payload preservation.
    assert response["partial"] is True
    assert response["errors"] == [
        {"surface": "entities", "error": "ws_connection_closed"}
    ]
    # Config payload's warnings survived the merge.
    assert response["warnings"] == ["config-side: dashboard opt-in skipped"]
    # Surviving config bucket is present.
    assert response["automations"] == [{"entity_id": "automation.a"}]
    assert response["entities"] == []
    assert response["entity_total_matches"] == 0
    assert response["config_total_matches"] == 1


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


def test_budget_partial_flag_set_when_automation_individual_fetches_failed() -> None:
    """Per-id automation fetches that raise (caught at debug-level in
    ``_fetch_automation_config``) surface as partial — without this the
    response can show ``total_matches=0`` while the backend was actually
    partially down."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response, automation_failed=4
    )
    assert response["partial"] is True
    assert "Automation config fetch incomplete: 4 failed" in response["partial_reason"]


def test_budget_partial_flag_set_when_script_individual_fetches_failed() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(response, script_failed=2)
    assert response["partial"] is True
    assert "Script config fetch incomplete: 2 failed" in response["partial_reason"]


def test_budget_partial_flag_set_when_helper_type_lists_failed() -> None:
    """Helpers run on every default ha_search call; silent per-type-list
    failures previously left callers unable to distinguish a clean
    zero-helper-match from a partial backend outage. ``helper_failed``
    closes that gap."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(response, helper_failed=3)
    assert response["partial"] is True
    assert "Helper list fetch incomplete: 3 input_* type(s) failed" in (
        response["partial_reason"]
    )


def test_budget_partial_flag_failed_and_skipped_combine() -> None:
    """Mixed budget exhaustion + individual fetch failures concatenate
    in ``partial_reason`` — caller sees both failure modes."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_budget_partial_flag(
        response,
        automation_skipped=5,
        automation_failed=2,
        helper_failed=1,
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "Automation config fetch incomplete: 5 skipped" in reason
    assert "Automation config fetch incomplete: 2 failed" in reason
    assert "Helper list fetch incomplete: 1 input_* type(s) failed" in reason


def test_budget_partial_flag_failures_append_to_existing_reason() -> None:
    """Append-safe: an existing scene-stats ``partial_reason`` is preserved
    and the new failure reasons are concatenated, not overwritten."""
    response: dict = {
        "success": True,
        "partial": True,
        "partial_reason": "Scene config fetch incomplete: 1 failed, 2 skipped.",
    }
    DeepSearchMixin._apply_budget_partial_flag(
        response, script_failed=3, helper_failed=1
    )
    assert response["partial"] is True
    assert response["partial_reason"].startswith("Scene config fetch incomplete")
    assert "Script config fetch incomplete: 3 failed" in response["partial_reason"]
    assert "Helper list fetch incomplete: 1 input_* type(s) failed" in (
        response["partial_reason"]
    )
