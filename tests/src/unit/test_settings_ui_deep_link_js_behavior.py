"""Deep links into the pending-approvals tab.

Drives the shipped Settings script through the JSDOM harness with the page's
own query string: ``?tab=tool-security-policies&token=<token>`` has to open the
tab, scroll to the card once the pending list has rendered, and say so when the
token names no pending request. JSDOM has no ``CSS.escape``, so the prelude
supplies one that actually escapes, the way a browser's does.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from ._js_harness import _assert_clean_init, run_script
from ._settings_ui_dom import DEFAULT_FETCHES
from ._settings_ui_js_helpers import localized_policy_dom, probe

_APP = "/settings"

_MESSAGES = {
    "invalid": "This approval token is no longer valid (already consumed or expired).",
}

_POLICY = {
    "version": 1,
    "schema_version": 1,
    "wait_seconds": 60,
    "approval_ttl_minutes": 5,
    "rule_effect": "require_approval",
    "rules": [],
    "event_decisions_enabled": False,
}

_ESCAPE_STUB = r"""
window.CSS = window.CSS || {};
window.CSS.escape = function (value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, function (ch) {
    return '\\' + ch;
  });
};
"""


def _page() -> str:
    """The policy-tab stub plus the tab button the deep link looks up."""
    return localized_policy_dom("en").replace(
        "</body>",
        '<div class="tabs" role="tablist">'
        '<button class="tab" data-panel="tool-security-policies">Policies</button>'
        "</div>"
        '<div class="panel" id="panel-tool-security-policies"></div>'
        "</body>",
    )


def _prelude(search: str, *, delay_ms: int = 0) -> str:
    script = (
        f"history.replaceState({{}}, '', {json.dumps(_APP + '?' + search)});"
        + _ESCAPE_STUB
        + "Element.prototype.scrollIntoView = function () {"
        "  window.__scrolled = this.getAttribute('data-pending-token') || '';"
        "};"
    )
    if delay_ms:
        script += (
            "(function () {"
            "  const realFetch = window.fetch;"
            "  window.fetch = function (url, opts) {"
            "    if (String(url).includes('/api/policy/pending')) {"
            "      return new Promise((resolve) => setTimeout("
            f"        () => resolve(realFetch(url, opts)), {delay_ms}));"
            "    }"
            "    return realFetch(url, opts);"
            "  };"
            "})();"
        )
    return script


def _entry(token: str) -> dict[str, Any]:
    return {
        "token": token,
        "tool_name": "ha_call_service",
        "args": {"domain": "lock"},
        "expires_at": "2026-11-04T12:00:00Z",
    }


def _fetches(token: str) -> dict[str, Any]:
    return {
        **DEFAULT_FETCHES,
        # A non-empty catalogue: the empty one raises an error toast that would
        # replace the message these tests read.
        "/api/settings/tools": {
            "status": 200,
            "json": {
                "tools": [
                    {
                        "name": "ha_get_state",
                        "title": "Get state",
                        "description": "Read an entity's state.",
                        "primary_tag": "Search",
                        "category": "read",
                    }
                ],
                "states": {},
            },
        },
        "/api/settings/features": {
            "status": 200,
            "json": {"flags": {"enable_tool_security_policies": {"value": True}}},
        },
        "/api/policy/config": {"status": 200, "json": _POLICY},
        "/api/policy/pending": {"status": 200, "json": {"pending": [_entry(token)]}},
    }


def _run(settings_script: str, search: str, *, delay_ms: int, invoke: str) -> Any:
    result = run_script(
        settings_script,
        initial_html=_page(),
        prelude=_prelude(search, delay_ms=delay_ms),
        fetch_map=_fetches("As2-pending"),
        invoke=invoke,
        settle_ms=5000,
        timeout_s=30,
    )
    _assert_clean_init(result)
    return result


def test_the_card_scrolls_once_the_pending_list_has_rendered(
    settings_script: str,
) -> None:
    """A fixed delay raced the queue: the scroll has to wait for the render."""
    result = _run(
        settings_script,
        "tab=tool-security-policies&token=As2-pending",
        delay_ms=1500,
        invoke=(
            "await new Promise((r) => setTimeout(r, 2500));"
            "document.body.dataset.scrolled = String(window.__scrolled || '');"
        ),
    )

    assert probe(result, "scrolled") == "As2-pending"


def test_a_crafted_token_does_not_scroll_to_another_card(
    settings_script: str,
) -> None:
    """The settings page's own URL is as untrusted as the panel's: the token
    must be escaped before it is put in a selector."""
    crafted = 'x"],[data-pending-token^="As2-'
    result = _run(
        settings_script,
        "tab=tool-security-policies&token=" + quote(crafted, safe=""),
        delay_ms=0,
        invoke=(
            "await new Promise((r) => setTimeout(r, 500));"
            "document.body.dataset.scrolled = String(window.__scrolled || '');"
        ),
    )

    assert probe(result, "scrolled") == ""


def test_a_token_that_names_no_pending_request_is_reported(
    settings_script: str,
) -> None:
    """Silence looks like a broken link: the page has to say the request is no
    longer pending."""
    result = _run(
        settings_script,
        "tab=tool-security-policies&token=not-pending",
        delay_ms=0,
        invoke=(
            "await new Promise((r) => setTimeout(r, 500));"
            "const toast = document.querySelector('.ha-toast-msg');"
            "document.body.dataset.toast = String((toast && toast.textContent) || '');"
        ),
    )

    assert _MESSAGES["invalid"] in (probe(result, "toast") or "")
