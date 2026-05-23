"""Behavioural tests for the rendered ``<script>`` body in ``settings_ui.py``.

The existing ``TestRenderedHTMLJsSyntax`` class only verifies that the
script body parses. These tests use the JSDOM harness at
``tests/js/harness.mjs`` to drive the script through realistic flows and
assert on observable side effects (HTTP calls issued, BroadcastChannel
messages emitted, DOM mutations, ``location.reload`` invocations).

The behaviours under test were introduced by PR #1420 (unified
addon-restart flow) and named in issue #1422 as the coverage gap a
client-side regression would otherwise leak into master with no test
failure.
"""

from __future__ import annotations

import pytest

from ._js_harness import extract_script_body, run_script

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def settings_script() -> str:
    """The rendered ``<script>`` body from ``_SETTINGS_HTML``.

    Module-scoped because the body is pure and large; re-extracting on
    every test would be wasted work.
    """
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)


# Minimum DOM the script touches during init (``loadFeatureFlags()`` and
# ``loadTools()`` run unconditionally at script tail) plus the restart
# UI elements every restart test asserts against. Building this once per
# test keeps each test independent without paying re-render cost.
MIN_DOM = """
<!DOCTYPE html>
<html><body>
  <div id="status"></div>
  <button id="restartBtn" style="display:none"></button>
  <div id="restartNotice"><span id="restartNoticeText"></span></div>
  <div id="sidecarStopRow" style="display:none"></div>
  <button id="stopSidecarBtn"></button>
  <input id="search" />
  <div id="groups"></div>
  <div id="summary"></div>
  <div id="backupConfigForm"></div>
  <div id="backupConfigActions"></div>
  <button id="backupConfigSave"></button>
  <div id="backupConfigStatus"></div>
  <div id="panel-tools" class="panel active"></div>
  <div id="panel-server" class="panel"></div>
  <div id="panel-backups" class="panel"></div>
</body></html>
"""

# Default routes for the init-time fetches. Individual tests merge in
# their own entries via ``{**DEFAULT_FETCHES, "/restart": ...}``.
DEFAULT_FETCHES: dict[str, dict] = {
    "/api/settings/features": {
        "status": 200,
        "json": {"features": [], "values": {}},
    },
    "/api/settings/tools": {
        "status": 200,
        "json": {"tools": [], "states": {}, "addon_mode": True},
    },
    "/api/settings/info": {
        "status": 200,
        "json": {"instance_id": "baseline-id"},
    },
    "/api/settings/backup-config": {
        "status": 200,
        "json": {},
    },
}


# ---------------------------------------------------------------------------
# restartAddon() flow
# ---------------------------------------------------------------------------


class TestRestartAddonFlow:
    """Covers the ``restartAddon`` concurrency guard, 4xx-suppress-reload
    branch, and 5xx fall-through path.
    """

    def test_concurrent_calls_issue_only_one_supervisor_restart(
        self, settings_script: str
    ) -> None:
        """The ``restartInProgress`` flag must block a second invocation.

        DevTools re-entry, accessibility tools, and cross-tab broadcasts
        can all queue a second ``restartAddon()`` call before the first
        finishes. Without the guard, each call would POST
        ``/api/settings/restart`` again — queueing redundant supervisor
        restarts and a redundant page reload.
        """
        # Info returns a baseline that never flips, so the probe loop
        # keeps running for the full RESTART_PROBE_MAX_TOTAL_MS window —
        # plenty of time for both restartAddon() invocations to race.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 200, "json": {}},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              window.restartAddon();
              window.restartAddon();
            """,
        )

        restart_posts = [
            f
            for f in result.fetches
            if "/api/settings/restart" in f["url"] and f["method"] == "POST"
        ]
        assert len(restart_posts) == 1, (
            f"expected exactly one POST to /api/settings/restart, "
            f"got {len(restart_posts)}: {restart_posts}"
        )

    def test_4xx_response_suppresses_reload_keeps_button_enabled(
        self, settings_script: str
    ) -> None:
        """4xx is a genuine config error (e.g. SUPERVISOR_TOKEN unset).

        Restart was NOT initiated → the page must stay, the user must
        see the failure message, and the button must remain enabled so
        they can retry after fixing the config. No broadcast — other
        tabs would only see a misleading "restart in progress".
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {
                "status": 400,
                "json": {"error": {"message": "SUPERVISOR_TOKEN unset"}},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
        )

        assert result.reloads == 0, "4xx response must not trigger a page reload"
        assert result.broadcasts_of_type("restart-initiated") == [], (
            "4xx response must not broadcast restart-initiated to other tabs"
        )
        # The error surfaces via alert() so the user is forced to read it.
        assert any("SUPERVISOR_TOKEN unset" in a for a in result.alerts), (
            f"expected alert with config error, got alerts={result.alerts}"
        )
        # Button must be re-enabled for retry.
        assert 'disabled=""' not in result.dom or "restartBtn" not in result.dom, (
            "button should be re-enabled after a 4xx (no disabled attr); "
            f"dom snippet: {result.dom[:500]}"
        )

    def test_5xx_response_falls_through_to_reload_cycle(
        self, settings_script: str
    ) -> None:
        """5xx means supervisor IS killing our upstream mid-response.

        The restart IS in flight even though we got an error code — the
        ingress dropped because our process is going away. Must fall
        through to the poll-and-reload cycle, not surface the 5xx as a
        config error.
        """
        # Pre-restart info → baseline; post-restart info → flipped.
        # The harness's fetch_map matches by substring on first hit; we
        # use a single entry and rely on the JS calling info() again
        # after restart fires.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 503, "body": ""},
            # Replace the default info route so the probe sees a NEW
            # instance_id and exits the polling loop with restarted=true.
            "/api/settings/info": {
                "status": 200,
                "json": {"instance_id": "new-id-after-restart"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
        )

        # With info always returning a "new" id, the probe finds the flip
        # almost immediately and triggers reload. The fact that the
        # restart endpoint returned 5xx must not short-circuit this.
        assert result.reloads >= 1, (
            f"5xx restart response must still trigger reload cycle, "
            f"reloads={result.reloads}, errors={result.errors}"
        )

    def test_probe_waits_for_instance_id_flip_not_just_200(
        self, settings_script: str
    ) -> None:
        """The whole point of the ``previousInstanceId`` plumbing.

        If supervisor silently fails to restart, the OLD process keeps
        answering 200. Pre-fix, the probe would see a 200 and reload —
        but the user would land back on the same broken instance. Post-
        fix, the probe must keep polling until ``instance_id`` differs.

        Here we force info to always return the same id as the baseline,
        confirm the probe DOES NOT reload, and confirm the manual-reload
        fallback UI lands (so the user isn't left in an indefinite
        spinner).
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 200, "json": {}},
            "/api/settings/info": {
                "status": 200,
                "json": {"instance_id": "baseline-id"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
            # 80s of virtual time covers the 60s probe window with margin.
            settle_ms=80000,
        )

        assert result.reloads == 0, (
            "probe must not reload when instance_id never flips — "
            "that would land the user back on the same broken instance"
        )
        # Manual-reload fallback message must surface.
        assert "did not come back online" in result.dom.lower(), (
            f"expected manual-reload fallback message, dom={result.dom[:600]}"
        )


# ---------------------------------------------------------------------------
# BroadcastChannel listener
# ---------------------------------------------------------------------------


class TestBroadcastChannelListener:
    """The cross-tab restart UX hinges on every open tab reacting to the
    originating tab's broadcasts. These tests pin the listener contract.
    """

    def test_restart_required_event_shows_notice_banner(
        self, settings_script: str
    ) -> None:
        """When ANY tab saves a flag needing restart, all tabs see the banner."""
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            broadcast_events=[
                {
                    "channel": "ha-mcp-settings",
                    "data": {"type": "restart-required"},
                    "delayMs": 50,
                },
            ],
        )

        # The notice div picks up the "show" class via classList.add.
        assert 'class="show"' in result.dom or "show" in result.dom, (
            f"restartNotice should have 'show' class after restart-required "
            f"broadcast; dom={result.dom[:600]}"
        )

    def test_restart_initiated_event_runs_reload_cycle_in_listening_tab(
        self, settings_script: str
    ) -> None:
        """The originating tab broadcasts ``restart-initiated`` so every
        OTHER tab runs its own poll-then-reload cycle. Without this, the
        non-originating tabs stay on a stale connection to a dead addon.
        """
        fetches = {
            **DEFAULT_FETCHES,
            # Probe sees the flip on first call after broadcast fires.
            "/api/settings/info": {
                "status": 200,
                "json": {"instance_id": "flipped"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            broadcast_events=[
                {
                    "channel": "ha-mcp-settings",
                    "data": {
                        "type": "restart-initiated",
                        "previousInstanceId": "baseline-id",
                    },
                    "delayMs": 50,
                },
            ],
        )

        assert result.reloads >= 1, (
            f"listening tab should reload after restart-initiated broadcast; "
            f"reloads={result.reloads}, errors={result.errors}"
        )


# ---------------------------------------------------------------------------
# saveFeatureFlag JSON-parse fallback
# ---------------------------------------------------------------------------


class TestSaveFeatureFlagJsonParseFallback:
    """Pins the contract for the truncated-body fallback in
    ``saveFeatureFlag``.

    A 200 OK with a body that can't be parsed as JSON used to silently
    skip the restart-required surface, leaving the user thinking the
    change was live when it actually requires a restart. The fallback
    defaults ``restart_required`` to true on that path.
    """

    def test_truncated_200_body_defaults_to_restart_required(
        self, settings_script: str
    ) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            # 200 OK + intentionally broken JSON body — the production
            # try/catch around resp.json() falls back to {restart_required: true}.
            "/api/settings/features": {
                "status": 200,
                "body": "{not-json",
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.saveFeatureFlag('foo', true);",
        )

        # restartNotice should have been shown.
        assert "show" in result.dom, (
            f"restartNotice should have 'show' class after truncated-body save; "
            f"dom={result.dom[:600]}"
        )
        # Cross-tab broadcast should have fired so other tabs surface the
        # banner too.
        assert result.broadcasts_of_type("restart-required"), (
            f"truncated-body save should broadcast restart-required, "
            f"got broadcasts={result.broadcasts}"
        )

    def test_error_response_does_not_default_to_restart_required(
        self, settings_script: str
    ) -> None:
        """The fallback only applies on ``resp.ok`` — error responses must
        surface the HTTP status, not silently claim success.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 500,
                "body": "{not-json",
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.saveFeatureFlag('foo', true);",
        )

        # No broadcast emitted on failure.
        assert not result.broadcasts_of_type("restart-required"), (
            f"failed save must not broadcast restart-required; "
            f"got broadcasts={result.broadcasts}"
        )
