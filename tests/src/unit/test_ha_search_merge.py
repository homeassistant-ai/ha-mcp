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
from ha_mcp.tools.smart_search._scenes import SceneSearchMixin
from ha_mcp.tools.tools_search import (
    _ALWAYS_KEEP_PROJECTION,
    _ENTITIES_BRANCH_SKIP_KEYS,
    _INTENT_SKIP_WARNING,
    _compute_eligibility,
    _emit_intent_skip_warning,
    _finalize_partial_state,
    _merge_payload_metadata,
    _mirror_partial_to_warnings,
    _project_response_fields,
    _synthesize_combined_pagination,
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
    _validate_search_types(["automation", "script", "scene", "helper", "dashboard"])


def test_validate_search_types_subset_passes() -> None:
    _validate_search_types(["scene"])


def test_validate_search_types_unknown_rejected() -> None:
    """A typo / stale type name like ``blueprint`` or ``frobnicate`` would
    previously return zero matches with no warning — now surfaces as
    ``VALIDATION_FAILED`` with ``parameter='search_types'``."""
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types(["frobnicate"])
    assert "frobnicate" in str(excinfo.value)


def test_validate_search_types_mixed_valid_invalid_rejected() -> None:
    """Pin that valid types from a mixed input are NOT echoed back in the
    error message's unknown-types list — only the actual unknown value
    appears, so the agent can correct the typo cleanly."""
    with pytest.raises(ToolError) as excinfo:
        _validate_search_types(["automation", "frobnicate", "scene"])
    err = str(excinfo.value)
    # The error message contains the Python repr of the unknown-only list.
    # If valid types leaked in, this exact-list match would fail.
    assert "['frobnicate']" in err, (
        f"Unknown-types list should contain exactly ['frobnicate']; got: {err}"
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
# distinct input combinations. Returns (registry_eligible, body_eligible,
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
    assert _gate(q="kitchen", dom="light", area="Kitchen", state="on", pin=True) == (
        False,
        True,
        False,
    )


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


def test_synthesize_combined_pagination_truth_table() -> None:
    """Pin S7(b) + S7(c)-pagination against the real
    ``_synthesize_combined_pagination`` helper. The previous tests
    reimplemented the OR-synthesis and next_offset pick inside the test
    body, so a regression at the real call site (e.g. ``or`` → ``and``)
    would have silently passed."""

    def _run(eh: bool, eno: int | None, ch: bool, cno: int | None) -> dict:
        response: dict = {
            "entity_has_more": eh,
            "entity_next_offset": eno,
            "config_has_more": ch,
            "config_next_offset": cno,
        }
        _synthesize_combined_pagination(response)
        return response

    # Neither surface has more — flat keys are both falsy.
    r = _run(False, None, False, None)
    assert r["has_more"] is False
    assert r["next_offset"] is None

    # Only entity has more — flat keys take the entity side.
    r = _run(True, 10, False, None)
    assert r["has_more"] is True
    assert r["next_offset"] == 10

    # Only config has more — flat keys take the config side.
    r = _run(False, None, True, 5)
    assert r["has_more"] is True
    assert r["next_offset"] == 5

    # Both have more — flat picks the entity value (first non-None via ``or``).
    r = _run(True, 10, True, 10)
    assert r["has_more"] is True
    assert r["next_offset"] == 10


def test_finalize_partial_state_extends_errors_not_clobbers() -> None:
    """Pin S7(c) against the real ``_finalize_partial_state``. The no-
    clobber contract is the heart of the A6 fix; a regression that
    re-introduces ``response["errors"] = errors_local`` (the original
    clobber) would now fail here, not silently pass an inline simulation."""
    response: dict = {
        "partial": False,
        "errors": [{"surface": "config-internal", "code": "BUDGET"}],
    }
    orchestrator_errors = [{"surface": "entities", "error": "ws_connection_closed"}]
    _finalize_partial_state(
        response, partial_local=True, errors_local=orchestrator_errors
    )
    assert response["partial"] is True
    # Both sets of errors must survive — payload errors first (already in
    # response from the merge), orchestrator surface errors appended.
    assert response["errors"] == [
        {"surface": "config-internal", "code": "BUDGET"},
        {"surface": "entities", "error": "ws_connection_closed"},
    ]


def test_finalize_partial_state_noop_when_no_branch_raised() -> None:
    """When the orchestrator-local partial is False (both branches returned
    cleanly), the response keeps whatever ``partial`` / ``errors`` the merge
    helper already accumulated — no overwrite."""
    response: dict = {
        "partial": True,
        "errors": [{"surface": "config-internal", "code": "BUDGET"}],
    }
    _finalize_partial_state(response, partial_local=False, errors_local=[])
    assert response["partial"] is True
    assert response["errors"] == [{"surface": "config-internal", "code": "BUDGET"}]


# Entity-intent skip warning emission ------------------------------------


def test_intent_skip_warning_emitted_when_gate_fires() -> None:
    """Pin S6-new: the gate-True path emits the entity-intent warning
    naming ``search_types=[...]`` as the opt-back-in mechanism. The
    previous test surface only covered the gate's True/False decision;
    nothing verified the warning actually reaches the response."""
    response: dict = {"warnings": []}
    _emit_intent_skip_warning(response, body_skipped_by_intent_gate=True)
    assert len(response["warnings"]) == 1
    assert response["warnings"][0] == _INTENT_SKIP_WARNING
    # The user-visible opt-back-in hint must be present; agents read this.
    assert "search_types=" in response["warnings"][0]


def test_intent_skip_warning_not_emitted_when_gate_quiet() -> None:
    response: dict = {"warnings": ["pre-existing"]}
    _emit_intent_skip_warning(response, body_skipped_by_intent_gate=False)
    assert response["warnings"] == ["pre-existing"]


def test_intent_skip_warning_preserves_existing_warnings() -> None:
    response: dict = {"warnings": ["from-entity-branch"]}
    _emit_intent_skip_warning(response, body_skipped_by_intent_gate=True)
    assert response["warnings"][0] == "from-entity-branch"
    assert response["warnings"][1] == _INTENT_SKIP_WARNING


# Top-level fields= projection -------------------------------------------
#
# Pins B1-new from the re-review: the `fields=` capability that
# `ha_search_entities` carried pre-rename is restored at the orchestrator
# layer. Always-keep covers all diagnostic / pagination keys so a
# projection can never hide incomplete-results state.


def _projection_response_fixture() -> dict:
    return {
        "success": True,
        "query": "kitchen",
        "entities": [{"entity_id": "light.kitchen"}],
        "entity_total_matches": 1,
        "automations": [{"entity_id": "automation.k"}],
        "scripts": [],
        "scenes": [],
        "helpers": [],
        "config_total_matches": 1,
        "search_types": ["automation", "script", "scene", "helper"],
        "count": 2,
        "offset": 0,
        "limit": 10,
        "has_more": False,
        "next_offset": None,
        "entity_has_more": False,
        "entity_next_offset": None,
        "config_has_more": False,
        "config_next_offset": None,
        "warnings": ["sample-warning"],
        "errors": [],
        "partial": False,
    }


def test_project_response_fields_none_returns_unchanged() -> None:
    response = _projection_response_fixture()
    result = _project_response_fields(response, None)
    assert result is response  # identity, not a copy


def test_project_response_fields_keeps_requested_bucket() -> None:
    """Requesting one bucket drops the other top-level buckets that aren't
    in the always-keep set — but diagnostic / pagination keys survive."""
    response = _projection_response_fixture()
    result = _project_response_fields(response, ["entities"])
    assert "entities" in result
    # Other buckets are top-level and not always-keep — dropped.
    assert "automations" not in result
    assert "scripts" not in result
    # Diagnostic / pagination keys always survive.
    for k in (
        "success",
        "warnings",
        "errors",
        "partial",
        "entity_total_matches",
        "config_total_matches",
        "has_more",
        "next_offset",
        "count",
        "offset",
        "limit",
    ):
        assert k in result, f"always-keep key {k!r} was dropped"


def test_project_response_fields_always_keep_protects_partial_state() -> None:
    """A caller passing fields=["entities"] can't accidentally hide
    `partial: True` / `partial_reason` / `errors[]` — the always-keep
    contract is the whole point of the projection."""
    response = _projection_response_fixture()
    response["partial"] = True
    response["partial_reason"] = "config-body budget exhausted: 5 skipped"
    response["errors"] = [{"surface": "entities", "error": "ws_lost"}]
    result = _project_response_fields(response, ["entities"])
    assert result["partial"] is True
    assert result["partial_reason"] == "config-body budget exhausted: 5 skipped"
    assert result["errors"] == [{"surface": "entities", "error": "ws_lost"}]


def test_project_response_fields_keeps_multiple_requested() -> None:
    response = _projection_response_fixture()
    result = _project_response_fields(response, ["entities", "automations"])
    assert "entities" in result
    assert "automations" in result
    assert "scripts" not in result
    assert "scenes" not in result


def test_project_response_fields_unknown_key_appends_typo_warning() -> None:
    """A caller requesting a key not present in the response gets a typo-
    guard warning appended to ``warnings[]`` listing the unknown keys and
    what's available — provided for free by delegating to
    ``util_helpers.project_fields``. Better UX than the previous silent
    drop: an agent that mistypes ``fields=["entitis"]`` gets a clear
    signal rather than a mysteriously empty response."""
    response = _projection_response_fixture()
    result = _project_response_fields(response, ["nonexistent_bucket"])
    assert "nonexistent_bucket" not in result
    assert "success" in result  # always-keep
    # Original warning entries preserved + typo warning appended.
    assert "sample-warning" in result["warnings"]
    assert any(
        "nonexistent_bucket" in w and "not found" in w for w in result["warnings"]
    ), f"expected typo-guard warning, got: {result['warnings']!r}"


# partial → warnings mirror ----------------------------------------------
#
# Pins the BAT-driven ask from the re-review: agents read warnings[]
# consistently but ignore partial / partial_reason. Mirroring the
# truncation reason into warnings makes incompleteness reach the
# user even when the agent drops the partial flag.


def test_mirror_partial_to_warnings_copies_reason_with_prefix() -> None:
    response: dict = {
        "partial": True,
        "partial_reason": "config-body budget exhausted: 5 automations skipped",
        "warnings": [],
    }
    _mirror_partial_to_warnings(response)
    assert response["warnings"] == [
        "incomplete results: config-body budget exhausted: 5 automations skipped"
    ]


def test_mirror_partial_to_warnings_noop_when_partial_false() -> None:
    response: dict = {
        "partial": False,
        "partial_reason": "should-not-mirror",
        "warnings": [],
    }
    _mirror_partial_to_warnings(response)
    assert response["warnings"] == []


def test_mirror_partial_to_warnings_noop_when_no_reason() -> None:
    """``partial: True`` without a reason text is unusual but tolerated —
    the mirror has nothing to surface, so it does nothing rather than
    appending an empty / misleading warning entry."""
    response: dict = {"partial": True, "warnings": []}
    _mirror_partial_to_warnings(response)
    assert response["warnings"] == []


def test_mirror_partial_to_warnings_idempotent_does_not_duplicate() -> None:
    """A response that already carries the mirror entry shouldn't grow
    a duplicate on a second mirror call — important because the
    orchestrator calls the mirror once but a future re-run inside a
    retry loop could otherwise double-append."""
    response: dict = {
        "partial": True,
        "partial_reason": "X failed",
        "warnings": ["incomplete results: X failed"],
    }
    _mirror_partial_to_warnings(response)
    assert response["warnings"] == ["incomplete results: X failed"]


def test_mirror_partial_to_warnings_preserves_existing_warnings() -> None:
    response: dict = {
        "partial": True,
        "partial_reason": "Y failed",
        "warnings": ["pre-existing warning"],
    }
    _mirror_partial_to_warnings(response)
    assert response["warnings"] == [
        "pre-existing warning",
        "incomplete results: Y failed",
    ]


# Scene fetch — registry-succeeded-zero-HA-managed case --------------
#
# Pin the fix for the re-review's "Investigate the 106/106 scene
# config-fetch failures" finding. The pre-fix `_select_scene_ids_to_fetch`
# conflated two cases via `if not homeassistant_scene_uids`: registry
# walk failed (legitimate fallback to attempt-all) AND registry walk
# succeeded with zero HA-managed scenes (every scene is integration-
# managed; attempting them 404s every one). The fix distinguishes via
# an explicit `registry_failed` parameter.


def test_select_scene_ids_registry_succeeded_zero_ha_managed_skips_all() -> None:
    """Registry succeeded but the fixture has only integration-managed
    scenes (no platform='homeassistant' entries). Pre-fix: fell back to
    attempt-all, every fetch 404'd, partial_reason reported `N scenes
    failed`. Post-fix: all counted as integration_skipped, zero fetched,
    no false partial."""
    scored = [
        ("scene.x", "X", "uid-x", 100),
        ("scene.y", "Y", "uid-y", 100),
    ]
    sids, integration_skipped = SceneSearchMixin._select_scene_ids_to_fetch(
        scored, configs={}, homeassistant_scene_uids=set(), registry_failed=False
    )
    assert sids == []
    assert integration_skipped == 2


def test_select_scene_ids_registry_failed_attempts_all() -> None:
    """When the registry walk actually failed, the existing fallback
    remains: attempt all scenes, accept false partials (better than
    dropping legitimate HA-managed scenes silently)."""
    scored = [
        ("scene.x", "X", "uid-x", 100),
        ("scene.y", "Y", "uid-y", 100),
    ]
    sids, integration_skipped = SceneSearchMixin._select_scene_ids_to_fetch(
        scored, configs={}, homeassistant_scene_uids=set(), registry_failed=True
    )
    assert sorted(sids) == ["uid-x", "uid-y"]
    assert integration_skipped == 0


def test_select_scene_ids_mixed_ha_and_integration_splits_correctly() -> None:
    """When some scenes are HA-managed and some integration-managed,
    fetch only HA ones; count integration scenes as skipped."""
    scored = [
        ("scene.x", "X", "uid-x", 100),
        ("scene.y", "Y", "uid-y", 100),
        ("scene.z", "Z", "uid-z", 100),
    ]
    sids, integration_skipped = SceneSearchMixin._select_scene_ids_to_fetch(
        scored,
        configs={},
        homeassistant_scene_uids={"uid-x"},
        registry_failed=False,
    )
    assert sids == ["uid-x"]
    assert integration_skipped == 2


def test_select_scene_ids_skips_already_fetched_configs() -> None:
    """Scenes whose config is already in the bulk-fetched dict are
    skipped from the per-id fetch regardless of integration / HA status."""
    scored = [
        ("scene.x", "X", "uid-x", 100),
        ("scene.y", "Y", "uid-y", 100),
    ]
    sids, integration_skipped = SceneSearchMixin._select_scene_ids_to_fetch(
        scored,
        configs={"uid-x": {"name": "X"}},
        homeassistant_scene_uids={"uid-x", "uid-y"},
        registry_failed=False,
    )
    assert sids == ["uid-y"]
    assert integration_skipped == 0


def test_always_keep_set_includes_all_diagnostic_and_pagination_keys() -> None:
    """Pin the always-keep set membership so an accidental removal of one
    of the diagnostic keys would fail loudly. ``success`` and ``warnings``
    are guaranteed by ``project_fields`` itself, so they are intentionally
    not in this orchestrator-side extension set."""
    required = {
        "errors",
        "partial",
        "partial_reason",
        "entity_total_matches",
        "config_total_matches",
        "has_more",
        "next_offset",
        "entity_has_more",
        "entity_next_offset",
        "config_has_more",
        "config_next_offset",
        "count",
        "offset",
        "limit",
    }
    missing = required - _ALWAYS_KEEP_PROJECTION
    assert not missing, f"always-keep set missing: {missing}"


# Per-type partial flag ---------------------------------------------------


def test_budget_partial_flag_set_when_automation_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=3, script_skipped=0
    )
    assert response["partial"] is True
    assert "Automation config fetch incomplete: 3 skipped" in response["partial_reason"]
    assert "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET" in response["partial_reason"]


def test_budget_partial_flag_set_when_script_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=0, script_skipped=7
    )
    assert response["partial"] is True
    assert "Script config fetch incomplete: 7 skipped" in response["partial_reason"]
    assert "HAMCP_SCRIPT_CONFIG_TIME_BUDGET" in response["partial_reason"]


def test_budget_partial_flag_combines_both_surfaces() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
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
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=3, script_skipped=0
    )
    assert response["partial"] is True
    assert response["partial_reason"].startswith("Scene config fetch incomplete")
    assert "Automation config fetch incomplete: 3 skipped" in response["partial_reason"]


def test_budget_partial_flag_noop_when_no_skips() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
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
    DeepSearchMixin._apply_per_type_partial_flag(response, automation_failed=4)
    assert response["partial"] is True
    assert "Automation config fetch incomplete: 4 failed" in response["partial_reason"]


def test_budget_partial_flag_set_when_script_individual_fetches_failed() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, script_failed=2)
    assert response["partial"] is True
    assert "Script config fetch incomplete: 2 failed" in response["partial_reason"]


def test_budget_partial_flag_set_when_helper_type_lists_failed() -> None:
    """Helpers run on every default ha_search call; silent per-type-list
    failures previously left callers unable to distinguish a clean
    zero-helper-match from a partial backend outage. ``helper_failed``
    closes that gap."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, helper_failed=3)
    assert response["partial"] is True
    assert (
        "Helper list fetch incomplete: 3 input_* type(s) failed"
        in (response["partial_reason"])
    )


def test_budget_partial_flag_failed_and_skipped_combine() -> None:
    """Mixed budget exhaustion + individual fetch failures concatenate
    in ``partial_reason`` — caller sees both failure modes."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
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
    DeepSearchMixin._apply_per_type_partial_flag(
        response, script_failed=3, helper_failed=1
    )
    assert response["partial"] is True
    assert response["partial_reason"].startswith("Scene config fetch incomplete")
    assert "Script config fetch incomplete: 3 failed" in response["partial_reason"]
    assert (
        "Helper list fetch incomplete: 1 input_* type(s) failed"
        in (response["partial_reason"])
    )


def test_entities_branch_skip_keys_strip_real_leak_set() -> None:
    """The orchestrator strips every entity sub-payload context key that the
    entities branch actually emits AND has no caller value at the envelope
    top. The strip set is empirically narrow: ``state_filter`` (input echo
    with no E2E coverage), ``area_name`` (per-entity decoration), and
    ``note`` (redundant mode-label already conveyed by ``search_type``).
    None are in ``_ALWAYS_KEEP_PROJECTION`` nor the ``fields=`` Available
    keys docstring, so leaking them would advertise undocumented keys via
    the typo-guard while a real ``fields=`` projection silently strips
    them.

    ``search_type``, ``domain_filter``, ``area_filter``, ``message``,
    ``by_domain``, ``state_filter_note``, and ``area_names`` are
    intentionally NOT stripped — the E2E test suite empirically pins
    their presence at the envelope top (callers verifiably depend on
    them), so all live in ``_ALWAYS_KEEP_PROJECTION`` + the docstring.

    The constant must also include the pagination plumbing
    (``results``/``total_matches``/``has_more``/``next_offset``) so the
    orchestrator can synthesise the per-surface ``entity_*`` versions from
    them without first-wins clobbering.
    """
    # Mirrors the real emission surface verified via grep on the
    # entities-branch (tools_search.py L1228/1517/1667 search_type;
    # L1231/1357/1421/1617 domain_filter; L1225/1339/1413/1713 area_filter;
    # L1233/1359/1423/1521/1674 state_filter; L1236/1646
    # state_filter_note; L1350 area_name; L1518 note; L1259/1388/1542/1665
    # by_domain). Phantoms (``area_id`` is loop var, ``suggestions`` is
    # ha_get_bulk_status only) are NOT included.
    payload = {
        "results": [{"entity_id": "light.kitchen"}],
        "total_matches": 1,
        "has_more": False,
        "next_offset": 0,
        "search_type": "exact_match",
        "domain_filter": "light",
        "area_filter": "Kitchen",
        "state_filter": "on",
        "area_name": "Kitchen",
        "note": "Listing all light entities (empty query with domain_filter)",
        "by_domain": {"light": [{"entity_id": "light.kitchen"}]},
        "state_filter_note": "fuzzy mode — total_matches is unfiltered count",
        "warnings": ["seeded warning"],
    }
    response: dict = {"success": True, "query": "kitchen", "warnings": []}
    _merge_payload_metadata(response, payload, skip_keys=_ENTITIES_BRANCH_SKIP_KEYS)

    # Stripped keys must not appear at top level.
    must_strip = {
        "results",
        "total_matches",
        "has_more",
        "next_offset",
        "state_filter",
        "area_name",
        "note",
    }
    leaked = sorted(must_strip & set(response))
    assert not leaked, (
        f"entity sub-payload context keys leaked into top-level response: {leaked}; "
        "extend _ENTITIES_BRANCH_SKIP_KEYS"
    )

    # E2E-pinned keys are intentionally kept (documented + always-keep).
    assert response.get("by_domain") == {"light": [{"entity_id": "light.kitchen"}]}, (
        "by_domain must survive the orchestrator merge — it is documented + "
        "in _ALWAYS_KEEP_PROJECTION as the toggle-gated feature output"
    )
    assert response.get("state_filter_note") == (
        "fuzzy mode — total_matches is unfiltered count"
    ), (
        "state_filter_note must survive the orchestrator merge — it explains "
        "why entity_total_matches differs from count under fuzzy + state_filter"
    )
    for kept in ("search_type", "domain_filter", "area_filter"):
        assert kept in response, (
            f"{kept!r} must survive the orchestrator merge — E2E suite "
            f"empirically pins its presence at the envelope top"
        )

    # warnings must still accumulate (it's NOT in skip_keys).
    assert response["warnings"] == ["seeded warning"]


def test_entity_context_keys_not_in_always_keep_projection() -> None:
    """The entity sub-payload CONTEXT keys (the ones added specifically to
    keep the entity-side search context from leaking) must not appear in
    ``_ALWAYS_KEEP_PROJECTION`` — otherwise a ``fields=`` projection would
    resurrect a key the orchestrator just stripped, defeating the strip.

    ``has_more``/``next_offset`` appear in both tuples by design: the
    skip-set drops the per-surface payload values before merge (so the
    orchestrator can synthesise combined + per-surface versions cleanly);
    the always-keep set then retains the synthesised combined keys post-
    merge under the same names. They share names but live in different
    semantic positions.
    """
    pagination_plumbing = {"results", "total_matches", "has_more", "next_offset"}
    context_only = set(_ENTITIES_BRANCH_SKIP_KEYS) - pagination_plumbing
    overlap = context_only & set(_ALWAYS_KEEP_PROJECTION)
    assert not overlap, (
        f"entity context keys appear in both _ENTITIES_BRANCH_SKIP_KEYS and "
        f"_ALWAYS_KEEP_PROJECTION: {overlap} — a fields= projection would "
        "resurrect them after the orchestrator strip, undoing the fix"
    )


def test_budget_partial_flag_uses_space_semicolon_space_separator() -> None:
    """`" ; "` is the standardised boundary between fragments — matches
    ``_merge_payload_metadata`` and ``_apply_scene_partial_flag``. A
    regression to ``", "`` or ``"\\n"`` would still pass the substring
    assertions in the other tests but break callers (Haiku in particular)
    that split on ``" ; "`` to enumerate failure modes. Pin it explicitly.
    """
    # Fragment-to-fragment within a single _apply_per_type_partial_flag call.
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=2, script_skipped=4
    )
    reason = response["partial_reason"]
    assert " ; " in reason, (
        f"per-type fragments must be joined with ' ; '; got {reason!r}"
    )
    assert ", " not in reason or reason.count(" ; ") >= 1, (
        f"expected ' ; ' separator, not ', '; got {reason!r}"
    )

    # Existing-reason-to-new-fragment boundary.
    seeded: dict = {
        "success": True,
        "partial": True,
        "partial_reason": "Scene config fetch incomplete: 1 failed, 2 skipped.",
    }
    DeepSearchMixin._apply_per_type_partial_flag(seeded, automation_skipped=3)
    assert (
        "skipped. ; Automation config fetch incomplete" in seeded["partial_reason"]
    ), (
        f"existing reason and new fragment must be joined with ' ; '; "
        f"got {seeded['partial_reason']!r}"
    )
