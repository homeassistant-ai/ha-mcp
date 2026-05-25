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
    # Tool Security Policies tab (#966): the master-toggle checkbox
    # mirrors the Server-Settings flag and posts to /api/settings/features;
    # the global-settings save button writes wait_seconds / TTL.
    "policy-master-toggle",
    "policy-save-global-btn",
    # Advanced settings panel (#1164) — Save button + status text +
    # the 5 section containers that loadAdvancedSettings() writes to
    # via innerHTML. Without container divs in MIN_DOM, renderSection
    # silently no-ops (getElementById returns null) and the
    # behavioural tests find an empty body.
    "advSaveBtn",
    "advSaveStatus",
    "advSaveRow",
    # Top-of-panel duplicate Save row (#1164 follow-up) — same handler
    # as the bottom row; status text mirrors between both so the user
    # sees the latest outcome whichever button they used.
    "advSaveBtnTop",
    "advSaveStatusTop",
    "advSaveRowTop",
    # Connection section was removed from the panel (#1164 follow-up);
    # advSearch is now the first rendered advanced section.
    "advSearch",
    "advOperations",
    "advToolsSurface",
    "advDiagnostics",
    # Beta features dedicated container (#1164 follow-up) — beta
    # master + sub-flags render here, NOT into featuresBody, so the
    # dangerous block sits at the bottom of panel-server.
    "betaBody",
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
        elif el_id == "policy-master-toggle":
            rows.append('<input id="policy-master-toggle" type="checkbox" />')
        elif el_id == "policy-save-global-btn":
            rows.append('<button id="policy-save-global-btn"></button>')
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
        # Seed the info endpoint with two baseline responses then a
        # flipped one. The harness's `responses: [...]` shape advances
        # per call and sticks on the last entry, so any number of
        # probe iterations beyond the first sees the flip — that's
        # what lets the probe terminate with restarted=true.
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


# ---------------------------------------------------------------------------
# Tool Security Policies tab (#966)
# ---------------------------------------------------------------------------


def _policy_panel_dom() -> str:
    """MIN_DOM stub plus the elements the policy tab queries.

    The script binds top-level handlers on policy-master-toggle /
    policy-save-global-btn (covered by MIN_DOM), but the bodies of
    policyLoadConfig / policyLoadPending fetch and write into
    policy-* sub-elements that don't appear in MIN_DOM. Add the
    minimum set so the policy-tab invocations don't throw on missing
    elements when we exercise them directly.
    """
    extras = """
      <div id="policy-pending-list"></div>
      <div id="policy-load-error" style="display:none"></div>
      <div id="policy-rules-empty" style="display:none"></div>
      <div id="policy-rules-list"></div>
      <input id="policy-wait-seconds" />
      <input id="policy-ttl-minutes" />
    """
    return MIN_DOM.replace("</body>", extras + "</body>")


class TestPolicyTabFlow:
    """Locks in the new condition-builder UX wiring: master toggle
    posts to the same feature-flag endpoint the Server-Settings tab
    uses, and the pending-list shows the right copy depending on
    whether the feature is on or off."""

    def test_master_toggle_change_posts_to_features_endpoint(
        self, settings_script: str
    ) -> None:
        """Clicking the master toggle on the Policies tab must POST
        ``{flags: {enable_tool_security_policies: true}}`` to
        ``/api/settings/features`` — same endpoint as the Server
        Settings checkbox. Without this the two surfaces would drift
        and the user couldn't trust the on-tab toggle to actually
        flip addon config."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {"restart_required": True},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              const cb = document.getElementById('policy-master-toggle');
              cb.checked = true;
              cb.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 50));
            """,
        )
        _assert_clean_init(result)
        flag_posts = [
            f
            for f in result.fetches
            if f["method"] == "POST" and "/api/settings/features" in f["url"]
        ]
        assert len(flag_posts) >= 1, (
            f"expected POST to /api/settings/features; got {result.fetches}"
        )
        # The body is JSON-serialised — parse and assert structurally
        # rather than substring-matching, so a body like
        # ``{"flags":{"x":"enable_tool_security_policies"}}`` (which
        # would pass the loose match) doesn't false-positive.
        import json

        matched = False
        for f in flag_posts:
            raw = f.get("body", "")
            if not raw:
                continue
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            flags = body.get("flags") if isinstance(body, dict) else None
            if (
                isinstance(flags, dict)
                and flags.get("enable_tool_security_policies") is True
            ):
                matched = True
                break
        assert matched, (
            "expected POST body containing "
            f"{{'flags': {{'enable_tool_security_policies': True}}}}; "
            f"got {[f.get('body') for f in flag_posts]}"
        )

    def test_pending_list_shows_off_message_when_feature_disabled(
        self, settings_script: str
    ) -> None:
        """When the addon flag is off, /api/policy/pending 503s. The
        UI should tell the user the feature is just turned off — NOT
        the misleading "sidecar / unavailable" text the earlier code
        showed. This catches regressions where the new copy gets
        clobbered back to the generic message."""
        fetches = {
            **DEFAULT_FETCHES,
            # Feature flag explicitly disabled
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {"value": False},
                    },
                },
            },
            # Stub policy config endpoints so policyLoadConfig doesn't 500
            "/api/policy/config": {
                "status": 200,
                "json": {"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": []},
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {"error": "irrelevant when flag is off"},
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.policyLoadConfig();
              await window.policyLoadPending();
            """,
        )
        _assert_clean_init(result)
        # `dom` is the full final-state document snapshot; grep the
        # pending-list region for the new off-state copy.
        assert "turned off" in result.dom.lower(), (
            f"expected 'turned off' in pending-list snapshot; "
            f"dom contains: {result.dom[-2000:]}"
        )

    def test_pending_list_shows_server_message_when_feature_on_but_503(
        self, settings_script: str
    ) -> None:
        """Feature is on but the queue is unreachable (sidecar mode or
        ImportError at startup). The server's 503 message should
        propagate verbatim so the user knows to check the addon log,
        instead of the generic "feature off" text."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_tool_security_policies": {"value": True},
                    },
                },
            },
            "/api/policy/config": {
                "status": 200,
                "json": {"wait_seconds": 60, "approval_ttl_minutes": 5, "rules": []},
            },
            "/api/policy/pending": {
                "status": 503,
                "json": {
                    "error": "Tool security policies live approvals are not active. "
                    "Check the addon log for ImportError / RuntimeError details."
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=_policy_panel_dom(),
            fetch_map=fetches,
            invoke="""
              await window.policyLoadConfig();
              await window.policyLoadPending();
            """,
        )
        _assert_clean_init(result)
        assert "addon log" in result.dom.lower(), (
            f"expected addon-log message in pending-list snapshot; "
            f"dom contains: {result.dom[-2000:]}"
        )


# ---------------------------------------------------------------------------
# Env-pinned tool rows (#1164)
# ---------------------------------------------------------------------------


class TestEnvPinnedToolRows:
    """Env-pinned tools (DISABLED_TOOLS / PINNED_TOOLS) must render with
    disabled inputs and a banner naming the env var so the user knows
    why the toggles are locked.
    """

    def test_env_pinned_disabled_tool_renders_locked_with_env_var_label(
        self, settings_script: str
    ) -> None:
        """A tool listed in DISABLED_TOOLS renders with all inputs disabled
        and shows 'env-pinned via DISABLED_TOOLS' in the row.

        The Tools tab must surface the env-pinned state so the user is not
        confused by a toggle that silently refuses to save.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_foo",
                            "title": "Foo",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {"ha_foo": "disabled"},
                    "env_pinned": {"ha_foo": "disabled"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        # The row must carry the env-pinned class.
        assert "env-pinned" in result.dom, (
            f"expected .env-pinned class on locked tool row; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # The banner must name the env var.
        assert "DISABLED_TOOLS" in result.dom, (
            f"expected 'DISABLED_TOOLS' in env-pinned banner; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # Specifically: the enabled-toggle input for ha_foo must carry
        # the HTML `disabled` attribute. Coarse `"disabled" in result.dom`
        # would also pass on CSS class names elsewhere in the page; pin
        # the regex to the actual input element.
        import re

        enabled_input_match = re.search(
            r'<input[^>]*data-field="enabled"[^>]*>',
            result.dom,
        )
        assert enabled_input_match is not None, (
            "expected enabled-toggle <input data-field='enabled'> in DOM"
        )
        assert " disabled" in enabled_input_match.group(0), (
            f"expected disabled attribute on enabled-toggle input; got: "
            f"{enabled_input_match.group(0)}"
        )

    def test_env_pinned_pinned_tool_renders_locked_with_env_var_label(
        self, settings_script: str
    ) -> None:
        """A tool listed in PINNED_TOOLS renders with toggles disabled
        and shows 'env-pinned via PINNED_TOOLS' in the row.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "status": 200,
                "json": {
                    "tools": [
                        {
                            "name": "ha_bar",
                            "title": "Bar",
                            "primary_tag": "Utilities",
                            "tags": ["Utilities"],
                            "description": "Another test tool",
                            "annotations": {},
                        }
                    ],
                    "states": {},
                    "env_pinned": {"ha_bar": "pinned"},
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)

        assert "env-pinned" in result.dom, (
            f"expected .env-pinned class on pinned-via-env tool row; "
            f"dom tail: {result.dom[-3000:]}"
        )
        assert "PINNED_TOOLS" in result.dom, (
            f"expected 'PINNED_TOOLS' in env-pinned banner; "
            f"dom tail: {result.dom[-3000:]}"
        )
        # Pinned-toggle input for ha_bar must be disabled.
        import re

        pin_input_match = re.search(
            r'<input[^>]*data-field="pinned"[^>]*>',
            result.dom,
        )
        assert pin_input_match is not None, (
            "expected pin-toggle <input data-field='pinned'> in DOM"
        )
        assert " disabled" in pin_input_match.group(0), (
            f"expected disabled attribute on pin-toggle input; got: "
            f"{pin_input_match.group(0)}"
        )


class TestAdvancedSectionRender:
    """JSDOM coverage for the new Advanced Settings sections (#1164 Chunk 2b)."""

    def test_locked_field_shows_env_var_name_in_banner(
        self, settings_script: str
    ) -> None:
        """Env-pinned advanced field renders with a banner naming the env var.

        Fixtures a search-section field — the connection section is
        no longer rendered in the panel (#1164 follow-up), so a
        section: "connection" field would be silently dropped.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "env",
                            "editable": False,
                            "min": 1,
                            "max": 100,
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "FUZZY_THRESHOLD" in result.dom, (
            f"expected env var name in locked-row banner; "
            f"dom tail: {result.dom[-2000:]}"
        )
        assert "adv-row" in result.dom and "locked" in result.dom, (
            "expected .adv-row.locked class"
        )

    def test_log_level_renders_as_select_with_choices(
        self, settings_script: str
    ) -> None:
        """str field with choices renders as <select>, not <input>."""
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "log_level",
                            "env_var": "LOG_LEVEL",
                            "value": "INFO",
                            "type": "str",
                            "section": "diagnostics",
                            "origin": "default",
                            "editable": True,
                            "choices": [
                                "DEBUG",
                                "INFO",
                                "WARNING",
                                "ERROR",
                                "CRITICAL",
                            ],
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        import re

        select_match = re.search(
            r'<select[^>]*data-adv-field="log_level"[^>]*>(.*?)</select>',
            result.dom,
            re.DOTALL,
        )
        assert select_match is not None, "expected <select> for log_level"
        assert "DEBUG" in select_match.group(1)
        assert "CRITICAL" in select_match.group(1)

    def test_int_field_emits_min_max_attrs(self, settings_script: str) -> None:
        # Same connection-removed migration as test_locked_field above.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "default",
                            "editable": True,
                            "min": 1,
                            "max": 100,
                        }
                    ]
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        import re

        m = re.search(
            r'<input[^>]*data-adv-field="fuzzy_threshold"[^>]*>',
            result.dom,
        )
        assert m is not None, "expected number input for fuzzy_threshold"
        assert 'min="1"' in m.group(0)
        assert 'max="100"' in m.group(0)


class TestAddonModeLockedBannerCopy:
    """Locked-banner copy must avoid 'unset env var' wording in addon mode.

    Addon operators have no env-var surface to unset — the var was set
    either by start.py (from /data/options.json) or by Supervisor. The
    standalone-mode copy "unset it to edit here" is actively
    misleading there (#1164 user feedback). When the features /
    advanced / backup endpoints return ``is_addon=true``, the banner
    must point at the addon Configuration tab instead.
    """

    def test_master_locked_banner_in_addon_mode_points_at_configuration(
        self, settings_script: str
    ) -> None:
        """Master `enable_beta_features` env-locked in addon mode renders
        copy pointing at addon Configuration toggles, not "unset env var".
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "env",
                            "editable": False,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        }
                    },
                    "beta_sub_flags": [
                        "enable_yaml_config_editing",
                        "enable_filesystem_tools",
                        "enable_custom_component_integration",
                        "enable_code_mode",
                        "enable_lite_docstrings",
                    ],
                    "is_addon": True,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # Standalone copy must NOT appear.
        assert "unset it to edit here" not in result.dom, (
            "addon-mode master banner still shows standalone 'unset env var' copy"
        )
        # Addon-aware copy must appear.
        assert "addon Configuration" in result.dom, (
            f"expected 'addon Configuration' hint; dom tail: {result.dom[-2000:]}"
        )

    def test_advanced_locked_banner_in_addon_mode_avoids_unset_copy(
        self, settings_script: str
    ) -> None:
        """Env-pinned advanced field in addon mode renders addon-runtime
        copy, not "unset env var".
        """
        # Use a search-section field — the connection section is no
        # longer rendered in the panel (#1164 follow-up), so a fixture
        # with section: "connection" would be silently dropped by
        # loadAdvancedSettings() and the assertion would fail with an
        # empty body.
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "fuzzy_threshold",
                            "env_var": "FUZZY_THRESHOLD",
                            "value": 70,
                            "type": "int",
                            "section": "search",
                            "origin": "env",
                            "editable": False,
                        }
                    ],
                    "is_addon": True,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "unset it to edit here" not in result.dom, (
            "addon-mode advanced banner still shows standalone 'unset env var' copy"
        )
        assert "addon runtime environment" in result.dom, (
            f"expected 'addon runtime environment' wording; "
            f"dom tail: {result.dom[-2000:]}"
        )

    def test_standalone_mode_still_uses_unset_copy(self, settings_script: str) -> None:
        """Regression guard: when is_addon is false (or omitted), the
        existing standalone "unset env var" copy must still render —
        the addon branch should NOT take over standalone deployments.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": True,
                            "origin": "env",
                            "editable": False,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        }
                    },
                    "beta_sub_flags": [],
                    "is_addon": False,
                },
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "unset it to edit here" in result.dom, (
            f"standalone-mode copy regressed; dom tail: {result.dom[-2000:]}"
        )


class TestBetaBlockRendersAtBottom:
    """Beta master + sub-flags render into the dedicated `betaBody` div,
    NOT featuresBody, so the dangerous block sits at the bottom of the
    Server Settings panel (#1164 follow-up).
    """

    def _payload(self) -> dict:
        return {
            "flags": {
                "enable_tool_search": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_TOOL_SEARCH",
                },
                "enable_beta_features": {
                    "value": True,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_BETA_FEATURES",
                },
                "enable_yaml_config_editing": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_YAML_CONFIG_EDITING",
                },
                "enable_filesystem_tools": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
                },
            },
            "beta_sub_flags": [
                "enable_yaml_config_editing",
                "enable_filesystem_tools",
                "enable_custom_component_integration",
                "enable_code_mode",
                "enable_lite_docstrings",
            ],
            "is_addon": False,
        }

    def test_non_beta_rows_render_into_featuresBody(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const fb = document.getElementById('featuresBody').innerHTML;
              const bb = document.getElementById('betaBody').innerHTML;
              window.__bodies = JSON.stringify({fb, bb});
            """,
        )
        _assert_clean_init(result)
        # Non-beta row goes to featuresBody only.
        assert "Enable tool search" in result.dom
        m = re.search(r'<tbody id="featuresBody">(.*?)</tbody>', result.dom, re.DOTALL)
        assert m is not None, "featuresBody tbody not found in dom"
        fb_content = m.group(1)
        assert "Enable tool search" in fb_content, (
            "tool-search row should land in featuresBody"
        )
        assert "Enable beta features" not in fb_content, (
            "beta master row leaked into featuresBody (should be in betaBody)"
        )

    def test_beta_rows_render_into_betaBody(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              // Read the live container HTML directly; avoids brittle
              // regex matching against nested <div>s in serialised dom.
              const bb = document.getElementById('betaBody');
              const fb = document.getElementById('featuresBody');
              window.__bb = bb ? bb.innerHTML : '';
              window.__fb = fb ? fb.innerHTML : '';
            """,
        )
        _assert_clean_init(result)
        # The harness serialises window globals into result.dom — pick
        # the recorded innerHTML out of the trailing <script> block the
        # JSDOM serialiser emits. Easier path: match the betaBody div in
        # the serialised dom with a balanced-enough heuristic (start tag
        # to next sibling-level marker).
        bb_match = re.search(
            r'<div id="betaBody">(.*?)</div>\s*</body>', result.dom, re.DOTALL
        )
        # MIN_DOM places betaBody as the second-to-last body child;
        # everything between its open tag and the closing body wrapper
        # is its content (incl. nested children) bar the trailing
        # </div>.
        if bb_match is None:
            # Fallback: search for the rendered master-row marker inside
            # the betaBody region by anchoring on its class name.
            assert 'class="feature-row beta-master-row"' in result.dom, (
                "beta master row never rendered anywhere"
            )
            assert 'class="feature-row beta-sub"' in result.dom, (
                "beta sub-row never rendered anywhere"
            )
            # Without a clean container slice we can't prove leakage —
            # but featuresBody is the only other plausible destination
            # and renderFeatureFlags wipes both with .innerHTML = ''.
            fb_match = re.search(
                r'<tbody id="featuresBody">(.*?)</tbody>', result.dom, re.DOTALL
            )
            fb_content = fb_match.group(1) if fb_match else ""
            assert "Enable beta features" not in fb_content, (
                "beta master row leaked into featuresBody"
            )
            return
        bb_content = bb_match.group(1)
        assert "Enable beta features" in bb_content, (
            "beta master row should land in betaBody"
        )
        assert "Enable YAML config editing" in bb_content, (
            "beta sub-flag should land in betaBody"
        )
        assert "Enable tool search" not in bb_content, (
            "non-beta row leaked into betaBody"
        )

    def test_beta_section_header_present_with_danger_styling(self) -> None:
        """Section header reads 'Beta features (dangerous)' so users see
        the category boundary; styling uses .beta-section-title.
        Asserted against the production HTML (the header lives in the
        static panel-server markup, not in any JS-rendered container).
        """
        from ha_mcp.settings_ui import _SETTINGS_HTML

        assert "Beta features (dangerous)" in _SETTINGS_HTML, (
            "beta section heading copy missing or changed in production HTML"
        )
        assert "beta-section-title" in _SETTINGS_HTML, (
            "beta section title class missing in production HTML"
        )

    def test_top_save_button_posts_same_payload_as_bottom(
        self, settings_script: str
    ) -> None:
        """Clicking either Save button posts to the same endpoint with
        the same dirty-fields payload, and the disabled+status mirrors
        to both rows so the user sees state on whichever they used.
        """
        adv_field = {
            "field": "log_level",
            "env_var": "LOG_LEVEL",
            "value": "INFO",
            "type": "str",
            "section": "diagnostics",
            "origin": "default",
            "editable": True,
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        }
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {
                "status": 200,
                "json": {"fields": [adv_field], "is_addon": False},
                "responses": [
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                    {"status": 200, "json": {"applied": {"log_level": "DEBUG"}}},
                    {"status": 200, "json": {"fields": [adv_field], "is_addon": False}},
                ],
            },
        }
        # Mutate the field then click the TOP button. Assert a POST went
        # out and that both status els carry the same final text.
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="""
              await new Promise(r => setTimeout(r, 200));
              const sel = document.querySelector(
                'select[data-adv-field="log_level"]'
              );
              if (sel) {
                sel.value = 'DEBUG';
                sel.dispatchEvent(new Event('change'));
              }
              document.getElementById('advSaveBtnTop').click();
              await new Promise(r => setTimeout(r, 200));
            """,
        )
        _assert_clean_init(result)
        posts = [
            f
            for f in result.fetches
            if "/api/settings/advanced" in f["url"] and f["method"] == "POST"
        ]
        assert len(posts) >= 1, (
            f"top save button did not POST; fetches: {result.fetches}"
        )

    def test_two_step_save_note_present(self) -> None:
        """The two-step save → restart note must render at the top of
        the Server Settings panel so users know one click is not enough.
        Asserted against production HTML (static markup in panel-server,
        not a JS-rendered container).
        """
        from ha_mcp.settings_ui import _SETTINGS_HTML

        assert "Two-step save" in _SETTINGS_HTML, (
            "two-step save note copy missing or changed in production HTML"
        )
        # The note must call out both steps.
        assert "Save advanced settings" in _SETTINGS_HTML, "save step missing"
        assert "Restart" in _SETTINGS_HTML, "restart step missing"
        # The CSS class hook the integration relies on.
        assert "adv-save-note" in _SETTINGS_HTML, "save-note class hook missing"

    def test_beta_master_help_text_contains_danger_warning(
        self, settings_script: str
    ) -> None:
        """`enable_beta_features` help-text must lead with the danger
        warning per #1164 user feedback — these features can permanently
        damage HA and users must be told before flipping the master.
        """
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {"status": 200, "json": self._payload()},
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        assert "PERMANENTLY DAMAGE" in result.dom, (
            "master beta help-text missing 'PERMANENTLY DAMAGE' warning"
        )
        assert "OWN RISK" in result.dom, (
            "master beta help-text missing 'OWN RISK' framing"
        )
        assert "backup" in result.dom.lower(), (
            "master beta help-text missing backup advice"
        )


class TestBetaMasterToggleLiveRender:
    """JSDOM coverage for live re-render on master flip (#1164 Chunk 3a)."""

    def _flags_payload(self, master_value: bool) -> dict:
        return {
            "flags": {
                "enable_beta_features": {
                    "value": master_value,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_BETA_FEATURES",
                },
                "enable_yaml_config_editing": {
                    "value": False,
                    "origin": "default",
                    "editable": True,
                    "type": "bool",
                    "env_var": "ENABLE_YAML_CONFIG_EDITING",
                },
            },
            "beta_sub_flags": ["enable_yaml_config_editing"],
        }

    def test_master_off_renders_subrow_dimmed(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": self._flags_payload(master_value=False),
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # The yaml-editing row must carry both beta-sub AND dimmed classes.
        assert "beta-sub dimmed" in result.dom or (
            "beta-sub" in result.dom and "dimmed" in result.dom
        ), f"expected dimmed beta-sub row; dom tail: {result.dom[-3000:]}"

    def test_master_on_renders_subrow_not_dimmed(self, settings_script: str) -> None:
        fetches = {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": self._flags_payload(master_value=True),
            },
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            invoke="await new Promise(r => setTimeout(r, 200));",
        )
        _assert_clean_init(result)
        # Row has beta-sub class but NOT dimmed.
        import re

        beta_sub_rows = re.findall(
            r'<div[^>]*class="[^"]*beta-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert beta_sub_rows, "expected at least one beta-sub row"
        for row_html in beta_sub_rows:
            assert "dimmed" not in row_html, (
                f"unexpected dimmed on master-on row: {row_html}"
            )


class TestCodeModeNesting:
    """JSDOM coverage for code-mode sub-numerics nested under
    enable_code_mode in the Beta section (#1164 Chunk 3b)."""

    def _payloads(self, master_on: bool, code_mode_on: bool) -> dict[str, dict]:
        """Build /api/settings/features + /api/settings/advanced
        responses for the two-gate matrix."""
        return {
            **DEFAULT_FETCHES,
            "/api/settings/features": {
                "status": 200,
                "json": {
                    "flags": {
                        "enable_beta_features": {
                            "value": master_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_BETA_FEATURES",
                        },
                        "enable_code_mode": {
                            "value": code_mode_on,
                            "origin": "default",
                            "editable": True,
                            "type": "bool",
                            "env_var": "ENABLE_CODE_MODE",
                        },
                    },
                    "beta_sub_flags": ["enable_code_mode"],
                },
            },
            "/api/settings/advanced": {
                "status": 200,
                "json": {
                    "fields": [
                        {
                            "field": "code_mode_max_duration",
                            "env_var": "CODE_MODE_MAX_DURATION",
                            "value": 30.0,
                            "type": "float",
                            "section": "beta_codemode",
                            "origin": "default",
                            "editable": True,
                            "min": 1.0,
                            "max": 300.0,
                        }
                    ]
                },
            },
        }

    def test_codemode_subrows_dimmed_when_master_off(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=False, code_mode_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        import re

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, f"expected codemode-sub row in DOM; tail: {result.dom[-2000:]}"
        # All code-mode-sub rows must be dimmed when master is off.
        for row in cm_rows:
            assert "dimmed" in row, (
                f"expected dimmed on codemode-sub row with master off: {row}"
            )

    def test_codemode_subrows_dimmed_when_code_mode_off(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=True, code_mode_on=False),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        import re

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, "expected codemode-sub row"
        for row in cm_rows:
            assert "dimmed" in row, (
                f"expected dimmed on codemode-sub row with code_mode off: {row}"
            )

    def test_codemode_subrows_enabled_when_both_gates_on(
        self, settings_script: str
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=self._payloads(master_on=True, code_mode_on=True),
            invoke="await new Promise(r => setTimeout(r, 300));",
        )
        _assert_clean_init(result)
        import re

        cm_rows = re.findall(
            r'<div[^>]*class="[^"]*codemode-sub[^"]*"[^>]*>',
            result.dom,
        )
        assert cm_rows, "expected codemode-sub row"
        for row in cm_rows:
            assert "dimmed" not in row, f"unexpected dimmed when both gates on: {row}"
