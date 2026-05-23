"""Behavioural tests for the rendered ``<script>`` body in ``settings_ui.py``.

Use the JSDOM harness at ``tests/js/harness.mjs`` to drive the script
through realistic flows and assert on observable side effects (HTTP
calls issued, BroadcastChannel messages emitted, DOM mutations,
``location.reload`` invocations).

These tests catch the "broken page with no in-page diagnostic" failure
class — script-level errors silently abort handler init and leave the
page stuck on its initial state with no signal in the UI. The
parse-time guard in ``test_rendered_scripts_parse.py`` catches syntax
breaks; this file catches behavioural regressions on top of it.
"""

from __future__ import annotations

import re

import pytest

from ._js_harness import HarnessResult, extract_script_body, run_script

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


# Element ids the script binds top-level handlers on. Drift between
# this set and what the production script reaches via
# ``document.getElementById(...).addEventListener(...)`` is checked at
# import time by ``_assert_min_dom_covers_handlers`` below, so a future
# handler that lands without a matching DOM stub fails the suite
# immediately with a clear message instead of letting tests silently
# fail at init.
_TOP_LEVEL_ELEMENT_IDS = [
    "status",
    "restartBtn",
    "restartNotice",
    "restartNoticeText",
    "sidecarStopRow",
    "stopSidecarBtn",
    "search",
    "groups",
    "summary",
    "featuresBody",
    "backupConfigForm",
    "backupConfigActions",
    "backupConfigSave",
    "backupConfigStatus",
    "backupRefresh",
    "backupBulkDelete",
    "backupDomain",
    "backupEntity",
    "backupState",
    "backupList",
    "modalBackdrop",
    "modalBody",
    "modalTitle",
    "modalClose",
    "panel-tools",
    "panel-server",
    "panel-backups",
]


def _build_min_dom() -> str:
    """Render an HTML document that supplies every element the script
    binds a top-level handler on, so the init pass doesn't throw on a
    missing-element addEventListener call.
    """
    rows = []
    for el_id in _TOP_LEVEL_ELEMENT_IDS:
        if el_id.startswith("backup") and el_id.endswith(("Domain", "Entity", "State")):
            rows.append(f'<select id="{el_id}"></select>')
        elif el_id in ("backupList", "featuresBody"):
            rows.append(f'<table><tbody id="{el_id}"></tbody></table>')
        elif el_id == "modalBackdrop":
            rows.append('<div id="modalBackdrop"><div id="modalBody"></div></div>')
        elif el_id == "modalBody":
            continue  # rendered as a child of modalBackdrop above
        elif el_id.startswith("panel-"):
            rows.append(f'<div id="{el_id}" class="panel"></div>')
        elif el_id in (
            "restartBtn",
            "stopSidecarBtn",
            "backupConfigSave",
            "backupRefresh",
            "backupBulkDelete",
            "modalClose",
        ):
            rows.append(f'<button id="{el_id}"></button>')
        elif el_id == "restartNotice":
            rows.append(
                '<div id="restartNotice"><span id="restartNoticeText"></span></div>'
            )
        elif el_id == "restartNoticeText":
            continue  # rendered as a child of restartNotice above
        elif el_id == "search":
            rows.append('<input id="search" />')
        else:
            rows.append(f'<div id="{el_id}"></div>')
    body = "\n  ".join(rows)
    return f"<!DOCTYPE html>\n<html><body>\n  {body}\n</body></html>"


MIN_DOM = _build_min_dom()


def _assert_min_dom_covers_handlers() -> None:
    """Fail at collection time if the production script's top-level
    ``document.getElementById(...)`` calls drift past
    ``_TOP_LEVEL_ELEMENT_IDS``. Without this, a production change that
    adds a new top-level ``getElementById(...).addEventListener(...)``
    would throw during JSDOM init, every restart-flow assertion in the
    file would silently fail with empty-side-effect outputs, and a
    maintainer would have to dig through ``result.errors`` to find the
    actual root cause.
    """
    from ha_mcp.settings_ui import _SETTINGS_HTML

    script = extract_script_body(_SETTINGS_HTML)
    # Top-level (not indented) getElementById(...) calls — handlers
    # bound at script load time. Indented calls are inside functions
    # and only run when those functions execute, which our tests
    # control.
    referenced = set(
        re.findall(r"^document\.getElementById\('([^']+)'\)", script, re.M)
    )
    missing = referenced - set(_TOP_LEVEL_ELEMENT_IDS)
    if missing:
        raise RuntimeError(
            "settings_ui.py top-level getElementById ids drifted past "
            f"_TOP_LEVEL_ELEMENT_IDS in this test file: {sorted(missing)}. "
            "Add them and rebuild MIN_DOM.",
        )


_assert_min_dom_covers_handlers()


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


def _assert_clean_init(result: HarnessResult) -> None:
    """Fail loud on any script-init or transpile error.

    Each test in this file asserts on side effects (fetches, broadcasts,
    reloads, alerts). When the script throws at init time the side
    effects come back empty, the side-effect assertion fires with a
    misleading message ("expected 1 POST, got 0"), and the actual root
    cause sits in ``result.errors``. Calling this at the top of every
    test surfaces the real error first.
    """
    init_errors = [
        e
        for e in result.errors
        if e.startswith(("script init:", "transpile failure", "invoke:", "jsdom error"))
    ]
    assert not init_errors, f"script failed to initialise: {init_errors}"


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
        # keeps running for the full probe-timeout window — plenty of
        # time for both restartAddon() invocations to race.
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
        _assert_clean_init(result)

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

        Restart was NOT initiated — the page must stay, the user must
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
            invoke="""
              await window.restartAddon();
              // Snapshot the button's post-flow state on body for the
              // assertion below — querying via JS is more robust than
              // string-slicing the serialised dom.
              const btn = document.getElementById('restartBtn');
              document.body.dataset.restartBtnDisabled = String(btn.disabled);
              document.body.dataset.restartBtnText = btn.textContent || '';
            """,
        )
        _assert_clean_init(result)

        assert result.reloads == 0, "4xx response must not trigger a page reload"
        assert result.broadcasts_of_type("restart-initiated") == [], (
            "4xx response must not broadcast restart-initiated to other tabs"
        )
        assert any("SUPERVISOR_TOKEN unset" in a for a in result.alerts), (
            f"expected alert with config error, got alerts={result.alerts}"
        )
        assert 'data-restart-btn-disabled="false"' in result.dom, (
            "restartBtn.disabled should be false after a 4xx retry path"
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
        # Three sequenced /api/settings/info responses: the script hits
        # this endpoint at script init (loadTools), again pre-POST in
        # restartAddon (baseline capture), and a third time in the probe
        # loop. The last entry sticks, so any additional probe iteration
        # also sees the flipped id.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/restart": {"status": 503, "body": ""},
            "/api/settings/info": {
                "responses": [
                    {"status": 200, "json": {"instance_id": "baseline-id"}},
                    {"status": 200, "json": {"instance_id": "baseline-id"}},
                    {"status": 200, "json": {"instance_id": "flipped-id"}},
                ],
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await window.restartAddon();",
        )
        _assert_clean_init(result)

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
        but the user would land back on the same broken instance.
        Post-fix, the probe must keep polling until ``instance_id``
        differs.

        Here we force info to always return the same id as the
        baseline, confirm the probe DOES NOT reload, and confirm the
        manual-reload fallback UI lands (so the user isn't left in an
        indefinite spinner).
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
        _assert_clean_init(result)

        assert result.reloads == 0, (
            "probe must not reload when instance_id never flips — "
            "that would land the user back on the same broken instance"
        )
        assert "did not come back online" in result.dom.lower(), (
            f"expected manual-reload fallback message, dom={result.dom[:600]}"
        )


# ---------------------------------------------------------------------------
# BroadcastChannel listener + null-guard
# ---------------------------------------------------------------------------


class TestBroadcastChannelListener:
    """The cross-tab restart UX hinges on every open tab reacting to the
    originating tab's broadcasts. These tests pin the listener contract,
    plus the null-guard that lets the page boot in browsing contexts
    where BroadcastChannel is unavailable.
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
        _assert_clean_init(result)

        # The notice div picks up the "show" class via classList.add.
        assert 'class="show"' in result.dom or "show" in result.dom, (
            "restartNotice should have 'show' class after restart-required broadcast"
        )

    def test_restart_initiated_event_runs_reload_cycle_in_listening_tab(
        self, settings_script: str
    ) -> None:
        """The originating tab broadcasts ``restart-initiated`` so every
        OTHER tab runs its own poll-then-reload cycle. Without this,
        the non-originating tabs stay on a stale connection to a dead
        addon.
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
        _assert_clean_init(result)

        assert result.reloads >= 1, (
            f"listening tab should reload after restart-initiated broadcast; "
            f"reloads={result.reloads}, errors={result.errors}"
        )

    def test_script_boots_without_broadcastchannel_global(
        self, settings_script: str
    ) -> None:
        """The ``typeof BroadcastChannel === 'function'`` null-guard at
        module init lets the page render in iframe / older-browser
        contexts where BroadcastChannel is undefined.

        Removing the guard would throw a ReferenceError during init and
        every page interaction (including the restart button, which is
        the user's recovery path) would fail.
        """
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            broadcast_channel_unavailable=True,
            invoke="""
              // Confirm the page reached interactive state — restartBtn
              // listener wired, no init throw.
              const btn = document.getElementById('restartBtn');
              document.body.dataset.boot = btn ? 'ok' : 'no-btn';
            """,
        )
        _assert_clean_init(result)
        assert 'data-boot="ok"' in result.dom, (
            "page must boot when BroadcastChannel is undefined"
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
        _assert_clean_init(result)

        assert "show" in result.dom, (
            "restartNotice should have 'show' class after truncated-body save"
        )
        assert result.broadcasts_of_type("restart-required"), (
            f"truncated-body save should broadcast restart-required, "
            f"got broadcasts={result.broadcasts}"
        )

    def test_error_response_does_not_default_to_restart_required(
        self, settings_script: str
    ) -> None:
        """The fallback only applies on ``resp.ok`` — error responses
        must surface the HTTP status, not silently claim success.
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
        _assert_clean_init(result)

        assert not result.broadcasts_of_type("restart-required"), (
            f"failed save must not broadcast restart-required; "
            f"got broadcasts={result.broadcasts}"
        )
