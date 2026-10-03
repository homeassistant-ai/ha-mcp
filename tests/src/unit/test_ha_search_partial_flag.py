"""``DeepSearchMixin._apply_per_type_partial_flag`` partial-flag and fragment wording.

Pins the ``partial`` / ``partial_reason`` text the per-type budget, failure and
timeout counters produce on a ``ha_search`` response.
"""

from __future__ import annotations

from ha_mcp.tools.smart_search._deep import DeepSearchMixin

# Per-type partial flag ---------------------------------------------------


def test_budget_partial_flag_set_when_automation_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=3, script_skipped=0
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "3 automation(s) not scanned (time budget exhausted)" in reason
    # The un-rationalisable triad — every reason must state plainly that
    # the entities were not scanned, their match status is unknown, and
    # the result is not exhaustive. PR #1529 R5 strengthened the wording
    # because the prior softer phrasing was rationalised away by blind
    # agents who reported the result as complete.
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason
    assert "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET" in reason


def test_budget_partial_flag_set_when_script_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=0, script_skipped=7
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "7 script(s) not scanned (time budget exhausted)" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason
    assert "HAMCP_SCRIPT_CONFIG_TIME_BUDGET" in reason


def test_budget_partial_flag_combines_both_surfaces() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_skipped=2, script_skipped=4
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "2 automation(s) not scanned" in reason
    assert "4 script(s) not scanned" in reason


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
    assert "3 automation(s) not scanned" in response["partial_reason"]


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
    reason = response["partial_reason"]
    assert "4 automation(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_set_when_script_individual_fetches_failed() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, script_failed=2)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "2 script(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_set_when_automation_yaml_skipped() -> None:
    """404 on the per-id endpoint flags YAML-defined automations as
    structurally unfetchable (a distinct class from a generic non-404
    fetch failure). Closes the find-references completeness honesty gap
    from PR #1529 R5: previously these were lumped into ``failed`` and the
    warning didn't tell callers the gap was structural rather than
    transient."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, automation_yaml_skipped=2)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "2 automation(s) not scanned" in reason
    assert "404" in reason
    assert "YAML-defined" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_set_when_script_yaml_skipped() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, script_yaml_skipped=5)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "5 script(s) not scanned" in reason
    assert "404" in reason
    assert "YAML-defined" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_distinguishes_yaml_skipped_from_failed() -> None:
    """When both a 404 (yaml_skipped) and a non-404 (failed) error occur
    on the same type, the two fragments must be reported separately so
    a caller can tell the structural class apart from the transient
    class. The transient class is retry-able; the structural class is
    not, and the prescribed fix differs."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_failed=3, automation_yaml_skipped=2
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "3 automation(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "2 automation(s) not scanned" in reason
    assert "404" in reason
    assert "YAML-defined" in reason
    # Two distinct per-type fragments → exactly one ` ; ` separator.
    assert reason.count(" ; ") == 1


def test_budget_partial_flag_set_when_automation_individual_fetches_timed_out() -> None:
    """Per-id fetches that hit INDIVIDUAL_CONFIG_TIMEOUT classify as a
    distinct ``timeout`` class (issue #1784): on HA servers that serve
    config reads serially, a concurrent batch's tail queues past the
    per-request timeout while every request would still return 200.
    Lumping those into "raised a non-404 error" sent users hunting for
    broken automations that don't exist; the timeout fragment must
    instead point at the batch-size/timeout knobs."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, automation_timeout=31)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "31 automation(s) not scanned" in reason
    assert "timed out" in reason
    assert "non-404" not in reason
    assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in reason
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_set_when_script_individual_fetches_timed_out() -> None:
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, script_timeout=4)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "4 script(s) not scanned" in reason
    assert "timed out" in reason
    assert "non-404" not in reason
    assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in reason
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_distinguishes_timeout_from_failed() -> None:
    """Timeout and non-timeout failures on the same type must surface as
    two separate fragments — the prescribed fixes differ (tune the
    concurrency knobs vs investigate the error in debug logs)."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response, automation_failed=3, automation_timeout=5
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "3 automation(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "5 automation(s) not scanned (per-id fetch timed out" in reason
    # Two distinct per-type fragments -> exactly one ` ; ` separator.
    assert reason.count(" ; ") == 1


def test_failed_fragment_carries_error_sample_when_provided() -> None:
    """The generic non-404 fragment names ONE representative error when the
    fetch path captured one (#1784 follow-up): the opaque "raised a non-404
    error" hid a trivially-diagnosable server-side failure — every per-id
    script fetch 500ing on a ``!secret`` reference in scripts.yaml —
    behind a debug-log dive. The sample makes the response itself carry the
    diagnosis; because an HTTP 500's body is aiohttp's generic placeholder
    (the ``!secret`` cause is HA-log-only), the static HA-log hint rides
    alongside the sample."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response,
        script_failed=27,
        script_failed_sample="HTTP 500: 500 Internal Server Error",
    )
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert (
        "27 script(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HTTP 500: 500 Internal Server Error)" in reason
    )
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason
    assert "`!secret` reference in the config file HA loads" in reason
    assert "in the Home Assistant log" in reason


def test_failed_fragment_unchanged_without_sample() -> None:
    """No captured sample → the exact prior wording, with no dangling
    ``e.g.`` (pins backward compatibility of the fragment)."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, automation_failed=4)
    reason = response["partial_reason"]
    assert "4 automation(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "e.g." not in reason


def test_failed_sample_is_per_type() -> None:
    """Automation and script samples ride their own fragments — no
    cross-type bleed."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response,
        automation_failed=1,
        automation_failed_sample="RuntimeError: automation boom",
        script_failed=2,
        script_failed_sample="HTTP 500: script boom",
    )
    reason = response["partial_reason"]
    assert "non-404 error; e.g. RuntimeError: automation boom)" in reason
    assert "non-404 error; e.g. HTTP 500: script boom)" in reason
    # The static HA-log hint is scoped to HTTP-500 samples: the 500 script
    # fragment carries it, the RuntimeError automation fragment does not.
    assert reason.count("in the Home Assistant log") == 1


def test_http_500_hint_only_on_http_500_sample() -> None:
    """The static HA-log hint rides an HTTP-500 sample (whose body can't name
    the cause) but not a non-500 sample, which already names its own type +
    message. Scoping it keeps every non-500 fragment byte-identical."""
    http500: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        http500, automation_failed=1, automation_failed_sample="HTTP 500: boom"
    )
    assert "in the Home Assistant log" in http500["partial_reason"]

    non500: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        non500,
        automation_failed=1,
        automation_failed_sample="HTTP 502: Bad Gateway",
    )
    assert "in the Home Assistant log" not in non500["partial_reason"]

    no_sample: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(no_sample, automation_failed=1)
    assert "in the Home Assistant log" not in no_sample["partial_reason"]


def test_bare_http_500_sample_still_carries_hint() -> None:
    """An empty-bodied 500 renders as colon-less ``HTTP 500`` (see
    ``summarize_fetch_error``); the hint predicate matches the bare form too,
    so the one sample the 500 diagnosis exists for can't silently drop it.
    Near-misses — another status, or a message merely mentioning 500 — must
    not match."""
    bare: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        bare, automation_failed=1, automation_failed_sample="HTTP 500"
    )
    assert "in the Home Assistant log" in bare["partial_reason"]

    for near_miss_sample in ("HTTP 5000: not a 500", "RuntimeError: REST 500 body"):
        near_miss: dict = {"success": True}
        DeepSearchMixin._apply_per_type_partial_flag(
            near_miss, automation_failed=1, automation_failed_sample=near_miss_sample
        )
        assert "in the Home Assistant log" not in near_miss["partial_reason"], (
            f"hint must not fire on {near_miss_sample!r}"
        )


def test_double_500_hint_rides_each_fragment() -> None:
    """Both per-type samples being 500s → each fragment carries its own hint
    (pins the previously-unspecified both-types-500 composition)."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(
        response,
        automation_failed=1,
        automation_failed_sample="HTTP 500: 500 Internal Server Error",
        script_failed=2,
        script_failed_sample="HTTP 500: 500 Internal Server Error",
    )
    assert response["partial_reason"].count("in the Home Assistant log") == 2


def test_budget_partial_flag_set_when_helper_type_lists_failed() -> None:
    """Helpers run on every default ha_search call; silent per-type-list
    failures previously left callers unable to distinguish a clean
    zero-helper-match from a partial backend outage. ``helper_failed``
    closes that gap (input_* WS lists AND the flow-helper config-entries
    list, both surfaced through the same counter)."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, helper_failed=3)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "3 helper backend(s) not scanned" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


def test_budget_partial_flag_set_when_dashboard_backend_failed() -> None:
    """Dashboard search (opt-in) swallowed list/config failures to empty.
    ``dashboard_failed`` routes a failed dashboard backend through the same
    partial path so an opt-in dashboard search can't report a complete-looking
    empty result when the list or a config fetch failed."""
    response: dict = {"success": True}
    DeepSearchMixin._apply_per_type_partial_flag(response, dashboard_failed=2)
    assert response["partial"] is True
    reason = response["partial_reason"]
    assert "2 dashboard(s) not scanned" in reason
    assert "match status is unknown" in reason
    assert "not exhaustive" in reason


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
    assert "5 automation(s) not scanned (time budget exhausted)" in reason
    assert "2 automation(s) not scanned (per-id fetch raised a non-404 error)" in reason
    assert "1 helper backend(s) not scanned" in reason


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
    assert "3 script(s) not scanned" in response["partial_reason"]
    assert "1 helper backend(s) not scanned" in response["partial_reason"]
