"""Layout, announcements and rendering of the pending-approvals list.

Drives the shipped Settings script through the JSDOM harness. The list element
holds either approval cards or a one-line status message, and the two need
different containers. It is polled while the Tool Security Policies tab is
open, so a poll must neither disturb the cards nor repeat an announcement.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ha_mcp.settings_ui import _SETTINGS_HTML
from ha_mcp.settings_ui._i18n import CATALOGS, DEFAULT_LOCALE

from ._js_harness import run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    _assert_clean_init,
    _localized_policy_dom,
    _probe,
)
from .test_settings_ui_js_behavior import settings_script as settings_script

_MESSAGES = CATALOGS[DEFAULT_LOCALE]["messages"]
_EMPTY = _MESSAGES["policies.pending.empty"]
_DISABLED = _MESSAGES["policies.pending.disabled"]
_OFFLINE = _MESSAGES["policies.pending.offline"].replace("{message}", "connection lost")
_ONE_PENDING = _MESSAGES["policies.pending.announcement"].replace("{count}", "1")

_QUEUE_DRAINED = {"status": 200, "json": {"pending": []}}
_QUEUE_DOWN = {"status": 503, "json": {"error": "queue down"}}


def _entry(token: str = "tok-1", **fields: Any) -> dict[str, Any]:
    return {
        "token": token,
        "tool_name": "ha_call_service",
        "args": {"domain": "lock"},
        "expires_at": "2026-11-04T12:00:00Z",
        **fields,
    }


def _queue(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"status": 200, "json": {"pending": list(entries)}}


def _fetches(pending: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    return {
        **DEFAULT_FETCHES,
        "/api/settings/features": {
            "status": 200,
            "json": {
                "flags": {"enable_tool_security_policies": {"value": enabled}},
            },
        },
        "/api/policy/pending": pending,
    }


def _run(settings_script: str, fetches: dict[str, Any], invoke: str) -> Any:
    result = run_script(
        settings_script,
        initial_html=_localized_policy_dom(DEFAULT_LOCALE),
        fetch_map=fetches,
        invoke=f"""
          const list = document.getElementById('policy-pending-list');
          const region = document.getElementById('policy-pending-announcement');
          await window.policyLoadConfig();
          {invoke}
        """,
    )
    _assert_clean_init(result)
    return result


@pytest.mark.parametrize(
    ("next_response", "expected_text", "expected_role", "expected_live"),
    [
        (_QUEUE_DRAINED, _EMPTY, "status", "polite"),
        (_QUEUE_DOWN, "queue down", "alert", "assertive"),
        ({"throw": "connection lost"}, _OFFLINE, "alert", "assertive"),
    ],
    ids=["queue-drained", "queue-unreachable", "server-offline"],
)
def test_pending_cards_leave_the_centered_empty_state_box(
    settings_script: str,
    next_response: dict,
    expected_text: str,
    expected_role: str,
    expected_live: str,
) -> None:
    """``backup-empty`` is the dashed, centred box for a one-line message.
    Left on the list while cards render, it centres the tool name, the
    argument JSON and the buttons, and draws a second box around them. The
    list is reused for every state, so a message that follows the cards
    has to get the box back, and a failure has to interrupt the reader."""
    result = _run(
        settings_script,
        _fetches({"responses": [_queue(_entry()), next_response]}),
        """
          await window.policyLoadPending();
          list.dataset.classWithCards = list.className;
          list.dataset.cardCount = String(
            list.querySelectorAll('[data-pending-token]').length
          );
          await window.policyLoadPending();
          list.dataset.classAfterCards = list.className;
          list.dataset.textAfterCards = list.textContent;
          list.dataset.roleAfterCards = region.getAttribute('role');
          list.dataset.liveAfterCards = region.getAttribute('aria-live');
        """,
    )

    assert _probe(result, "card-count") == "1"
    assert "backup-empty" not in (_probe(result, "class-with-cards") or "")
    assert "backup-empty" in (_probe(result, "class-after-cards") or "")
    assert expected_text in (_probe(result, "text-after-cards") or "")
    assert _probe(result, "role-after-cards") == expected_role
    assert _probe(result, "live-after-cards") == expected_live


def test_status_changes_are_announced_once_with_failures_assertive(
    settings_script: str,
) -> None:
    """The list is polled and is not itself a live region, so the announcement
    has to be written once per change: rewriting it on every poll would make a
    screen reader repeat an unchanged outage."""
    result = _run(
        settings_script,
        _fetches(
            {
                "responses": [
                    _QUEUE_DOWN,
                    _QUEUE_DOWN,
                    _QUEUE_DRAINED,
                    _queue(_entry()),
                ]
            }
        ),
        """
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

    assert _probe(result, "outage-writes") == "1"
    assert _probe(result, "outage-role") == "alert"
    assert _probe(result, "outage-live") == "assertive"
    assert _probe(result, "outage-text") == "queue down"
    assert _probe(result, "empty-role") == "status"
    assert _probe(result, "empty-live") == "polite"
    assert _probe(result, "empty-text") == _EMPTY
    assert _probe(result, "text-with-cards") == _ONE_PENDING


def test_pending_list_shows_off_message_when_feature_disabled(
    settings_script: str,
) -> None:
    """When the flag is off, /api/policy/pending 503s. The UI should say the
    feature is just turned off, NOT the misleading "sidecar / unavailable"
    text the earlier code showed. A deliberate setting is no failure, so this
    is the one message that stays in the box without interrupting the reader."""
    result = _run(
        settings_script,
        _fetches(
            {"status": 503, "json": {"error": "irrelevant when flag is off"}},
            enabled=False,
        ),
        """
          await window.policyLoadPending();
          list.dataset.offText = list.textContent;
          list.dataset.offClass = list.className;
          list.dataset.offRegionText = region.textContent;
          list.dataset.offRole = region.getAttribute('role');
          list.dataset.offLive = region.getAttribute('aria-live');
        """,
    )

    assert _probe(result, "off-text") == _DISABLED
    assert "backup-empty" in (_probe(result, "off-class") or "")
    assert _probe(result, "off-region-text") == _DISABLED
    assert _probe(result, "off-role") == "status"
    assert _probe(result, "off-live") == "polite"


def test_a_poll_of_the_same_queue_keeps_focus_on_the_decision_button(
    settings_script: str,
) -> None:
    """Rebuilding unchanged cards removes the focused Approve or Deny button
    from the DOM and drops focus to ``<body>``, so a keyboard or screen-reader
    user would have only until the next poll to reach and press it."""
    result = _run(
        settings_script,
        _fetches(_queue(_entry())),
        """
          await window.policyLoadPending();
          const button = list.querySelector('[data-policy-action="approve"]');
          button.focus();
          list.dataset.focusBeforePoll = String(document.activeElement === button);
          await window.policyLoadPending();
          list.dataset.focusAfterPoll = String(document.activeElement === button);
        """,
    )

    assert _probe(result, "focus-before-poll") == "true"
    assert _probe(result, "focus-after-poll") == "true"


def test_a_replaced_approval_is_announced_though_the_count_is_unchanged(
    settings_script: str,
) -> None:
    """One approval decided and another arriving between two polls leaves the
    count, and so the announcement text, identical. The reader still has to
    hear about the new approval, and only about that."""
    result = _run(
        settings_script,
        _fetches(
            {
                "responses": [
                    _queue(_entry("tok-1")),
                    _queue(_entry("tok-1")),
                    _queue(_entry("tok-2")),
                ]
            }
        ),
        """
          let writes = 0;
          const observer = new MutationObserver((records) => { writes += records.length; });
          observer.observe(region, {childList: true, characterData: true, subtree: true});
          await window.policyLoadPending();
          await window.policyLoadPending();
          list.dataset.writesSameQueue = String(writes);
          await window.policyLoadPending();
          list.dataset.writesNewQueue = String(writes);
          list.dataset.renderedToken =
            list.querySelector('[data-pending-token]').dataset.pendingToken;
          observer.disconnect();
        """,
    )

    assert _probe(result, "writes-same-queue") == "1"
    assert _probe(result, "writes-new-queue") == "2"
    assert _probe(result, "rendered-token") == "tok-2"


def test_a_shortened_deadline_is_shown_and_announced_without_taking_focus(
    settings_script: str,
) -> None:
    """A dynamic-target approval can have its deadline cut after it is queued,
    with the token unchanged. Keeping the old text would promise the person
    deciding more time than the approval has left, and rebuilding the card
    would take the button from under them just as time runs short. With focus
    held on that button, only an announcement tells a screen-reader user."""
    shortened = "2026-11-04T11:01:00Z"
    result = _run(
        settings_script,
        _fetches(
            {
                "responses": [
                    _queue(_entry()),
                    _queue(_entry(expires_at=shortened)),
                ]
            }
        ),
        """
          await window.policyLoadPending();
          const button = list.querySelector('[data-policy-action="deny"]');
          button.focus();
          await window.policyLoadPending();
          list.dataset.shownExpiry =
            list.querySelector('.policy-pending-expires').textContent;
          list.dataset.focusKept = String(document.activeElement === button);
          list.dataset.announcedExpiry = region.textContent;
          list.dataset.expiryLive = region.getAttribute('aria-live');
        """,
    )

    expiry = _MESSAGES["policies.pending.expires"].replace("{time}", shortened)
    assert _probe(result, "shown-expiry") == expiry
    assert _probe(result, "focus-kept") == "true"
    assert _probe(result, "announced-expiry") == f"ha_call_service: {expiry}"
    assert _probe(result, "expiry-live") == "polite"


def test_every_shortened_deadline_is_announced(settings_script: str) -> None:
    """Two approvals can share a tool name and be cut to the same deadline, one
    poll apart, so the second announcement has identical text. Cut in one poll,
    both have to be read out rather than one standing in for the other."""
    first_cut = "2026-11-04T11:01:00Z"
    second_cut = "2026-11-04T11:00:30Z"
    result = _run(
        settings_script,
        _fetches(
            {
                "responses": [
                    _queue(_entry("tok-1"), _entry("tok-2")),
                    _queue(_entry("tok-1", expires_at=first_cut), _entry("tok-2")),
                    _queue(
                        _entry("tok-1", expires_at=first_cut),
                        _entry("tok-2", expires_at=first_cut),
                    ),
                    _queue(
                        _entry("tok-1", expires_at=second_cut),
                        _entry("tok-2", expires_at=second_cut),
                    ),
                ]
            }
        ),
        """
          await window.policyLoadPending();
          let writes = 0;
          const observer = new MutationObserver((records) => { writes += records.length; });
          observer.observe(region, {childList: true, characterData: true, subtree: true});
          await window.policyLoadPending();
          await window.policyLoadPending();
          list.dataset.writesForIdenticalText = String(writes);
          await window.policyLoadPending();
          list.dataset.bothAnnounced = region.textContent;
          observer.disconnect();
        """,
    )

    expiry = _MESSAGES["policies.pending.expires"].replace("{time}", second_cut)
    assert _probe(result, "writes-for-identical-text") == "2"
    assert (
        _probe(result, "both-announced")
        == f"ha_call_service: {expiry} ha_call_service: {expiry}"
    )


def test_cards_return_after_an_outage_with_the_same_queue(
    settings_script: str,
) -> None:
    """An outage replaces the cards with a message while the queue itself may
    not change, so the queue seen before it must not count as already shown."""
    result = _run(
        settings_script,
        _fetches({"responses": [_queue(_entry()), _QUEUE_DOWN, _queue(_entry())]}),
        """
          await window.policyLoadPending();
          await window.policyLoadPending();
          await window.policyLoadPending();
          list.dataset.cardCount = String(
            list.querySelectorAll('[data-pending-token]').length
          );
          list.dataset.classAfterOutage = list.className;
          list.dataset.announcedAfterOutage = region.textContent;
          list.dataset.liveAfterOutage = region.getAttribute('aria-live');
        """,
    )

    assert _probe(result, "card-count") == "1"
    assert "backup-empty" not in (_probe(result, "class-after-outage") or "")
    assert _probe(result, "announced-after-outage") == _ONE_PENDING
    assert _probe(result, "live-after-outage") == "polite"


def test_agent_supplied_approval_fields_stay_inert_text(
    settings_script: str,
) -> None:
    """``tool_name``, ``args`` and ``token`` come from the agent's call, so
    markup in them must not run on the page where a human grants approval."""
    token = 'tok"><img src=x onerror="window.pwned=1">'
    result = _run(
        settings_script,
        _fetches(
            _queue(
                _entry(
                    token,
                    tool_name='<img src=x onerror="window.pwned=1">',
                    args={"note": "</pre><script>window.pwned=1</script>"},
                )
            )
        ),
        f"""
          await window.policyLoadPending();
          list.dataset.injected = String(
            list.querySelectorAll('img, script').length
          );
          list.dataset.buttons = String(
            list.querySelectorAll('button[data-policy-token]').length
          );
          list.dataset.tokenIntact = String(
            list.querySelector('[data-pending-token]').dataset.pendingToken
              === {json.dumps(token)}
          );
        """,
    )

    assert _probe(result, "injected") == "0"
    assert _probe(result, "buttons") == "2"
    assert _probe(result, "token-intact") == "true"


def test_page_ships_a_polite_region_and_the_centered_empty_list() -> None:
    """The JSDOM fixture hand-writes these elements, so only this sees the
    markup a browser receives: without the live-region attributes the first
    message is lost, without the hiding class every announcement shows on the
    page, and without its class the empty list starts unstyled."""
    region = re.search(r'<div id="policy-pending-announcement"[^>]*>', _SETTINGS_HTML)
    empty_list = re.search(r'<div id="policy-pending-list"[^>]*>', _SETTINGS_HTML)

    assert region is not None
    assert 'class="visually-hidden"' in region.group(0)
    assert 'role="status"' in region.group(0)
    assert 'aria-live="polite"' in region.group(0)
    assert empty_list is not None
    assert 'class="backup-empty"' in empty_list.group(0)
