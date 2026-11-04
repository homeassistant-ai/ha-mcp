"""Layout of the pending-approvals list on the Tool Security Policies tab.

Drives the shipped Settings script through the JSDOM harness. The list element
holds either approval cards or a one-line status message, and the two need
different containers.
"""

from __future__ import annotations

import pytest

from ._js_harness import run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    _assert_clean_init,
    _policy_panel_dom,
    _probe,
)
from .test_settings_ui_js_behavior import settings_script as settings_script


@pytest.mark.parametrize(
    ("next_response", "expected_text"),
    [
        ({"status": 200, "json": {"pending": []}}, "No pending approvals."),
        ({"status": 503, "json": {"error": "queue down"}}, "queue down"),
    ],
    ids=["queue-drained", "queue-unreachable"],
)
def test_pending_cards_leave_the_centered_empty_state_box(
    settings_script: str,
    next_response: dict,
    expected_text: str,
) -> None:
    """``backup-empty`` is the dashed, centred box for a one-line message.
    Left on the list while cards render, it centres the tool name, the
    argument JSON and the buttons, and draws a second box around them. The
    list is reused for every state, so a message that follows the cards
    has to get the box back."""
    fetches = {
        **DEFAULT_FETCHES,
        "/api/settings/features": {
            "status": 200,
            "json": {
                "flags": {"enable_tool_security_policies": {"value": True}},
            },
        },
        "/api/policy/pending": {
            "responses": [
                {
                    "status": 200,
                    "json": {
                        "pending": [
                            {
                                "token": "tok-1",
                                "tool_name": "ha_call_service",
                                "args": {"domain": "lock"},
                                "expires_at": "2026-11-04T12:00:00Z",
                            }
                        ]
                    },
                },
                next_response,
            ]
        },
    }
    result = run_script(
        settings_script,
        initial_html=_policy_panel_dom(),
        fetch_map=fetches,
        invoke="""
          const list = document.getElementById('policy-pending-list');
          await window.policyLoadConfig();
          await window.policyLoadPending();
          list.dataset.classWithCards = list.className;
          list.dataset.cardCount = String(
            list.querySelectorAll('[data-pending-token]').length
          );
          await window.policyLoadPending();
          list.dataset.classAfterCards = list.className;
          list.dataset.textAfterCards = list.textContent;
        """,
    )
    _assert_clean_init(result)

    assert _probe(result, "card-count") == "1"
    assert "backup-empty" not in (_probe(result, "class-with-cards") or "")
    assert "backup-empty" in (_probe(result, "class-after-cards") or "")
    assert expected_text in (_probe(result, "text-after-cards") or "")


def test_status_changes_are_announced_once_with_failures_assertive(
    settings_script: str,
) -> None:
    """The list re-renders on a 3s poll and is not itself a live region, so the
    announcement has to be written once per change: rewriting it on every poll
    would make a screen reader repeat an unchanged outage."""
    outage = {"status": 503, "json": {"error": "queue down"}}
    fetches = {
        **DEFAULT_FETCHES,
        "/api/settings/features": {
            "status": 200,
            "json": {"flags": {"enable_tool_security_policies": {"value": True}}},
        },
        "/api/policy/pending": {
            "responses": [
                outage,
                outage,
                {"status": 200, "json": {"pending": []}},
                {
                    "status": 200,
                    "json": {
                        "pending": [
                            {
                                "token": "tok-1",
                                "tool_name": "ha_call_service",
                                "args": {},
                                "expires_at": "2026-11-04T12:00:00Z",
                            }
                        ]
                    },
                },
            ]
        },
    }
    result = run_script(
        settings_script,
        initial_html=_policy_panel_dom(),
        fetch_map=fetches,
        invoke="""
          const region = document.getElementById('policy-pending-announcement');
          const list = document.getElementById('policy-pending-list');
          await window.policyLoadConfig();
          let writes = 0;
          const observer = new MutationObserver((records) => { writes += records.length; });
          observer.observe(region, {childList: true, characterData: true, subtree: true});
          await window.policyLoadPending();
          await window.policyLoadPending();
          list.dataset.outageWrites = String(writes);
          list.dataset.outageRole = region.getAttribute('role');
          list.dataset.outageLive = region.getAttribute('aria-live');
          list.dataset.outageText = region.textContent;
          await window.policyLoadPending();
          list.dataset.emptyRole = region.getAttribute('role');
          list.dataset.emptyLive = region.getAttribute('aria-live');
          list.dataset.emptyText = region.textContent;
          await window.policyLoadPending();
          list.dataset.textWithCards = region.textContent;
          observer.disconnect();
        """,
    )
    _assert_clean_init(result)

    assert _probe(result, "outage-writes") == "1"
    assert _probe(result, "outage-role") == "alert"
    assert _probe(result, "outage-live") == "assertive"
    assert _probe(result, "outage-text") == "queue down"
    assert _probe(result, "empty-role") == "status"
    assert _probe(result, "empty-live") == "polite"
    assert _probe(result, "empty-text") == "No pending approvals."
    assert _probe(result, "text-with-cards") == ""
