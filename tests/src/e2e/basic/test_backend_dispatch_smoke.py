"""Multi-layer smoke tests for backend dispatch correctness.

The three e2e CI lanes set env vars that ``conftest.ha_container_with_fresh_config``
reads to choose a backend:

| Lane                          | HAOS_TEST_IMAGE_PATH | HAOS_TEST_MODE | expected backend |
| ----------------------------- | -------------------- | -------------- | ---------------- |
| e2e-tests.yml (testcontainer) | unset                | unset          | ``container``    |
| haos-e2e-tests.yml (external) | set                  | unset          | ``haos``         |
| haos-e2e-inaddon-tests.yml    | set                  | ``inaddon``    | ``haos_inaddon`` |
| haos-e2e-embedded-tests.yml   | set                  | ``embedded``   | ``haos_embedded``|

The testcontainer ``embedded`` backend (#1527) is a fourth variant selected by a
separate axis, ``E2E_BACKEND=embedded`` (not ``HAOS_TEST_MODE``): same container
HA, but the server-under-test is the in-process MCP server entry inside
the container.

Three layers of guard, each catching a different silent-failure mode:

1. ``test_backend_dispatch_matches_workflow_env`` — asserts the ``backend``
   field that conftest sets matches what the workflow env vars imply.
   Catches dispatch falling through to the wrong code path.

2. ``test_supervisor_addon_tool_behavior_matches_backend`` — calls
   ``ha_get_addon`` (which only works when a real Supervisor is present)
   and asserts success-vs-failure matches the claimed backend. Catches
   the case where conftest reports ``backend=X`` but actually a different
   HA instance is running (e.g. mock Supervisor on testcontainer making
   addon calls succeed when the real backend has no Supervisor at all,
   or HAOS reporting itself as container).

3. ``test_session_collected_test_count_above_floor`` — asserts the
   session collected at least the baseline number of tests. Catches
   collection-time regressions where a test file fails to import and
   pytest silently drops tens of tests.

4. ``test_session_skipped_count_below_ceiling`` — per-lane skip-count
   ceiling. Catches the inverse of #3: collection size unchanged but
   tests transition pass→skip silently because a marker was applied
   too broadly. The conftest documents a prior incident of this kind
   (PR #1375 audit, 14 ``supervisor_mock`` tests silently skipping on
   every testcontainer run — see the PR #1375 audit comment in ``tests/src/e2e/conftest.py``).

This file is placed under ``basic/`` (NOT ``haos_only/``) on purpose: the
auto-applied ``haos_only`` marker would skip these whenever
``is_haos_backend_selected()`` returns False, which is exactly the
silent-failure case we want to catch. ``basic/`` has no auto-applied
markers so the tests run unconditionally on every lane.
"""

from __future__ import annotations

import os
from typing import Any

from ..utilities.assertions import safe_call_tool

# Floor for total collected tests across all lanes. As of 2026-05-22
# container lanes collect 912 and HAOS lanes collect 915 (small per-mode
# variance from parametrize/fixture-driven cases). Floor sits well below
# all three to allow normal test-add/remove churn while still catching
# the case where ~50+ tests vanish from collection.
_COLLECTION_FLOOR = 850

# Per-lane ceilings for the count of skip-marked tests. Set 5-9 above
# current per-lane skip counts (as of 2026-05-22: container=46,
# haos=14, haos_inaddon=39 — the latter bumped by #1403's 17 new
# auto-backup tests that skip on haos_inaddon via @external_only).
# A buffer of 5-9 absorbs PRs that legitimately add a few new
# marker-gated tests, but catches a mass-skip incident like PR #1375
# (14 tests started skipping silently because a marker was applied
# too broadly).
_SKIP_CEILING_PER_LANE = {
    # The 5 dashboard-screenshot E2E tests
    # (tests/src/e2e/haos_only/test_dashboard_screenshot_addon.py) are marked
    # haos_only + inaddon_only: they RUN only on the haos_inaddon lane (the
    # only place the MCP server shares the Supervisor network and can reach the
    # screenshot engine), and SKIP on both the container lane (haos_only) and
    # the external-haos lane (inaddon_only). So both those ceilings gain 5;
    # haos_inaddon is unchanged because the tests run there.
    # The 5 dashboard-screenshot SIDECAR E2E tests
    # (tests/src/e2e/tools/test_dashboard_screenshot_sidecar.py) are marked
    # container_only: they RUN only on the container lane (in-process server +
    # fake engine) and SKIP on both HAOS lanes. So haos and haos_inaddon each
    # gain 5; container is unchanged because the tests run there.
    # Baselines are the observed skip counts as of 2026-05-22 (container=46,
    # haos=14, haos_inaddon=39 from the prose above), plus this PR's new
    # marker-gated skips, plus a 5-9 growth buffer.
    "container": 71,  # was 68; +3 in-process MCP server HAOS tests (haos_only/test_embedded_server_haos.py, skip on the container lane)
    "haos": 37,  # was 35; +2 in-process MCP server tests (workflows/embedded, @container_only, run on the container lane only). The haos_only embedded HAOS tests RUN on this lane, so they add no skips here.
    "haos_inaddon": 59,  # was 58; +1 net from the in-process MCP server tests (workflows/embedded, @container_only; observed lane count). The haos_only embedded HAOS tests RUN on this lane, so they add no skips here.
    # Embedded backend (#1527, E2E_BACKEND=embedded). Skips exactly the container
    # lane's marker-skips PLUS two embedded-specific additions:
    #   - haos_only + inaddon_only tests skip on embedded just like on container
    #     (embedded is neither HAOS nor inaddon), and
    #   - external_only tests skip here (their server is out-of-process, unreachable
    #     by test-process env/monkeypatch — same reason as inaddon), plus the
    #     workflows/embedded smoke test (not_on_embedded).
    # Static def-level derivation (Docker-less, so parametrize item-inflation isn't
    # visible locally): haos_only 51 + inaddon_only-outside-haos 11 + external_only
    # 35 (auto_backup 18, supervisor_mock 15, self_update_notice 1, file_operations
    # 1) + not_on_embedded 2 = 99. Initially set to 115 as a buffer for
    # parametrize item-inflation; round 6 (run 28709196071) observed the exact
    # item count and the entry below is pinned to it.
    "embedded": 119,  # observed exact count (round 6, run 28709196071): haos/inaddon-lane skips + the external_only in-process-server class (alternative coverage on the container lane) + the 2 self-referential smoke tests
    # HAOS embedded backend (#1527, HAOS_TEST_MODE=embedded). A HAOS lane, so it
    # skips the SAME set as the external HAOS lane (container_only + inaddon_only)
    # PLUS two haos_embedded-specific additions:
    #   - external_only tests skip here (their server is out-of-process inside the
    #     HAOS core container, unreachable by test-process env/monkeypatch — same
    #     reason as inaddon/container-embedded; alternative coverage on the external
    #     HAOS lane, where the in-process FastMCP server IS in the test process), and
    #   - the 3 haos_only embedded smoke tests skip (not_on_haos_embedded) because
    #     the session backend already enables the entry + drives the server.
    # Static def-level derivation (Docker/HAOS-less locally, so parametrize
    # item-inflation isn't visible): container_only 11 + inaddon_only 19 +
    # external_only 39 + smoke 3 = 72 (no overlaps: no external_only test is also
    # container_only/inaddon_only, and the 2 not_on_embedded tests are already
    # container_only). Applying the ~1.16x parametrize inflation the other HAOS
    # lanes show (haos def 30 → ~35 observed; haos_inaddon def 50 → ~58) gives
    # ~84; initially set to 90 with a small buffer, and round 8 observed
    # exactly 90 — the entry below is pinned to the observed count.
    "haos_embedded": 90,  # observed exact count (round 8): haos-lane skips + external_only + inaddon_only + the 3 lane-aware smoke tests
}


def test_backend_dispatch_matches_workflow_env(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Conftest dispatch must pick the backend the workflow env implies.

    Runs unconditionally on every lane — branches off env vars to
    approximate conftest's dispatch logic. (Conftest's
    ``is_haos_backend_selected`` additionally requires the qcow2 file
    to exist on disk; this test only checks env-var truthiness. The
    test deliberately re-derives the expected backend independently
    rather than calling the same helper, so a helper-side regression
    can't make the test silently agree with the bug.) Mismatch means
    the dispatch silently picked a different backend than CI asked for.
    """
    image_path = os.environ.get("HAOS_TEST_IMAGE_PATH")
    mode = os.environ.get("HAOS_TEST_MODE", "")
    backend = ha_container_with_fresh_config["backend"]

    if image_path and mode == "inaddon":
        assert backend == "haos_inaddon", (
            f"Workflow set HAOS_TEST_IMAGE_PATH + HAOS_TEST_MODE=inaddon "
            f"but dispatch picked backend={backend!r}. The inaddon "
            f"integration is NOT being exercised by this run."
        )
        addon_mcp_url = ha_container_with_fresh_config.get("addon_mcp_url")
        assert addon_mcp_url and addon_mcp_url.startswith("http"), (
            f"haos_inaddon backend reported but addon_mcp_url is "
            f"{addon_mcp_url!r}. mcp_client fixtures will route to the "
            f"wrong endpoint."
        )
        assert ha_container_with_fresh_config["container"] is None
        assert ha_container_with_fresh_config["port"] is None
        assert ha_container_with_fresh_config["config_path"] is None
    elif image_path and mode == "embedded":
        # haos_embedded (#1527): a HAOS backend whose server-under-test is the
        # baked in-process MCP server, driven over its ingress webhook on the
        # booted VM. Container keys are None (HAOS path); addon_mcp_url is None
        # (not the addon path); embedded_webhook_url is the connect URL.
        assert backend == "haos_embedded", (
            f"Workflow set HAOS_TEST_IMAGE_PATH + HAOS_TEST_MODE=embedded "
            f"but dispatch picked backend={backend!r}. The in-process "
            f"the in-process MCP server is NOT the server-under-test for this run."
        )
        assert ha_container_with_fresh_config["container"] is None
        assert ha_container_with_fresh_config["port"] is None
        assert ha_container_with_fresh_config["config_path"] is None
        assert ha_container_with_fresh_config.get("addon_mcp_url") is None
        webhook_url = ha_container_with_fresh_config.get("embedded_webhook_url")
        assert webhook_url and webhook_url.startswith("http"), (
            f"haos_embedded backend reported but embedded_webhook_url is "
            f"{webhook_url!r}; the mcp_client fixture would route nowhere."
        )
    elif image_path:
        assert backend == "haos", (
            f"Workflow set HAOS_TEST_IMAGE_PATH but dispatch picked "
            f"backend={backend!r}. The lane silently fell through to "
            f"the testcontainer path; tests are running against the "
            f"wrong HA instance."
        )
        assert ha_container_with_fresh_config["container"] is None
        assert ha_container_with_fresh_config["port"] is None
        assert ha_container_with_fresh_config["config_path"] is None
        assert ha_container_with_fresh_config["addon_mcp_url"] is None
    elif os.environ.get("E2E_BACKEND", "").strip().lower() == "embedded":
        # Embedded backend (#1527): a testcontainer variant (no HAOS env) whose
        # server-under-test is the in-process MCP server entry inside the
        # same container. It reuses the whole testcontainer path, so container /
        # port / config_path are populated exactly like the container backend, but
        # it exposes the ingress webhook URL that mcp_client connects to.
        assert backend == "embedded", (
            f"E2E_BACKEND=embedded set but dispatch picked backend={backend!r}. "
            f"The in-process MCP server entry is NOT the server-under-test "
            f"for this run."
        )
        assert ha_container_with_fresh_config["container"] is not None
        assert ha_container_with_fresh_config["port"] is not None
        assert ha_container_with_fresh_config["config_path"] is not None
        webhook_url = ha_container_with_fresh_config.get("embedded_webhook_url")
        assert webhook_url and webhook_url.startswith("http"), (
            f"embedded backend reported but embedded_webhook_url is "
            f"{webhook_url!r}; the mcp_client fixture would route nowhere."
        )
        # The embedded backend is not the addon path — no addon_mcp_url.
        assert ha_container_with_fresh_config.get("addon_mcp_url") is None
    else:
        assert backend == "container", (
            f"No HAOS env vars set, expected testcontainer backend, "
            f"got backend={backend!r}."
        )
        assert ha_container_with_fresh_config["container"] is not None
        assert ha_container_with_fresh_config["port"] is not None
        assert ha_container_with_fresh_config["config_path"] is not None
        # The container branch does NOT include an addon_mcp_url key at all,
        # unlike the HAOS branches. Use .get() so the assertion holds against
        # either absence or None.
        assert ha_container_with_fresh_config.get("addon_mcp_url") is None


async def test_supervisor_addon_tool_behavior_matches_backend(
    mcp_client: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Behavioral cross-check: ``ha_get_addon`` must succeed on HAOS, fail on container.

    Stronger guarantee than the dispatch-field check: conftest could
    self-report ``backend=container`` but actually have HAOS running
    (or vice versa). A real Supervisor only exists on HAOS — the HA
    Core testcontainer has no Supervisor service running. So:

    - HAOS external + inaddon: ``ha_get_addon`` returns a populated
      addons list (the bake installs several addons).
    - testcontainer: ``ha_get_addon`` raises ToolError
      (RESOURCE_NOT_FOUND from the ``supervisor/api`` WebSocket proxy
      because no Supervisor is running); ``safe_call_tool`` catches
      and decodes the structured error to ``{"success": False, ...}``.
      The dict conversion is load-bearing — a future maintainer should
      NOT switch to ``assert_mcp_failure`` or similar.

    The asymmetry of this check makes it impossible for one backend to
    impersonate the other while keeping this test green.
    """
    backend = ha_container_with_fresh_config["backend"]
    result = await safe_call_tool(mcp_client, "ha_get_addon", {})

    # All HAOS backends run against a real Supervisor, so ha_get_addon succeeds —
    # including haos_embedded, whose in-process server reaches Supervisor through
    # HA Core's supervisor/api WS proxy (it runs standalone with HA_MCP_EMBEDDED,
    # so it does not use SUPERVISOR_TOKEN directly, but the proxy path still works
    # because HA Core itself is supervised).
    if backend in ("haos", "haos_inaddon", "haos_embedded"):
        assert result.get("success") is True, (
            f"ha_get_addon failed on {backend} backend; Supervisor must "
            f"be running. Result: {result!r}"
        )
        # list_addons returns ``{"success": True, "addons": [...], "summary": {...}}``
        # — ``addons`` is a top-level key, not nested under ``data``.
        addons = result.get("addons") or []
        assert isinstance(addons, list) and len(addons) > 0, (
            f"Expected installed addons on {backend} (the bake installs "
            f"Node-RED, ESPHome, AppDaemon, dev addon, etc.); got "
            f"{addons!r}"
        )
    else:
        # testcontainer has no Supervisor → ha_get_addon must fail
        assert result.get("success") is False, (
            f"ha_get_addon unexpectedly succeeded on {backend} backend. "
            f"Testcontainer has no Supervisor service; success here "
            f"means we're actually running on HAOS but conftest reported "
            f"backend={backend!r}. Result: {result!r}"
        )


def test_session_collected_test_count_above_floor(request: Any) -> None:
    """Session collected at least ``_COLLECTION_FLOOR`` tests.

    Catches collection-time regressions: a test file fails to import,
    pytest collects tens of fewer tests, the suite stays green with
    reduced coverage. Collection count varies by a handful across
    modes (parametrize/fixture-driven — currently 912 container vs
    915 HAOS lanes); the floor sits well below all three lanes' actuals.
    """
    total = len(request.session.items)
    assert total >= _COLLECTION_FLOOR, (
        f"Only {total} tests collected, expected >= {_COLLECTION_FLOOR}. "
        f"A test file likely failed to import, dropping coverage. Check "
        f"for collection errors in the pytest output."
    )


def test_session_skipped_count_below_ceiling(
    request: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Per-lane skip-count must stay below ``_SKIP_CEILING_PER_LANE[backend]``.

    Catches the inverse of the collection-floor check: the suite still
    collects the expected total, but tests transition pass→skip silently
    because a marker was applied too broadly in conftest's
    ``pytest_collection_modifyitems`` hook.

    The conftest itself documents a real prior incident of this kind
    (the PR #1375 audit comment in ``tests/src/e2e/conftest.py`` — 14
    ``supervisor_mock`` tests silently skipping on every testcontainer
    run because an ``external_only`` skip was scoped wrong). A
    skip-count ceiling per lane catches that whole class of bug.

    Ceilings sit 5-9 above current per-lane skip counts; updates are
    only required when a PR legitimately introduces enough new
    marker-gated tests to cross the threshold (uncommon).
    """
    backend = ha_container_with_fresh_config["backend"]
    ceiling = _SKIP_CEILING_PER_LANE.get(backend)
    assert ceiling is not None, (
        f"Unknown backend {backend!r} — add to _SKIP_CEILING_PER_LANE "
        f"at the top of this file"
    )
    # Items in request.session.items already have skip markers applied
    # by pytest_collection_modifyitems (which ran before any test).
    # Under pytest-xdist with --dist loadscope, each worker collects
    # the full session, so this count is consistent per worker.
    skipped = sum(
        1
        for item in request.session.items
        if any(m.name == "skip" for m in item.iter_markers())
    )
    assert skipped <= ceiling, (
        f"{skipped} tests have skip markers on the {backend} lane, "
        f"which exceeds the ceiling of {ceiling}. A marker may be "
        f"applied too broadly in pytest_collection_modifyitems — "
        f"check pytest_collection_modifyitems in tests/src/e2e/conftest.py for recent changes. "
        f"If the increase is intentional (legitimate new marker-gated "
        f"tests), bump _SKIP_CEILING_PER_LANE[{backend!r}] in this file."
    )
