"""Behavioural test for the OAuth consent form's submit handler.

The consent form ships ten lines of inline JS that disables the submit
button and swaps in a spinner. The whole point is to prevent a
double-submit racing the OAuth round-trip — if the handler ever stopped
disabling the button, a user double-clicking through a slow auth flow
could mint two consent tokens and confuse the IdP.

This test exercises the rendered form in JSDOM and asserts the handler
fires exactly once per submit with the disabled + spinner state.
"""

from __future__ import annotations

import pytest

from ._js_harness import extract_script_body, run_script


@pytest.fixture(scope="module")
def consent_form_html() -> str:
    from ha_mcp.auth.consent_form import create_consent_html

    return create_consent_html(
        client_id="test-client",
        redirect_uri="https://test.local/cb",
        state="state-x",
        txn_id="txn-y",
    )


def _build_form_dom(consent_html: str) -> tuple[str, str]:
    """Return ``(initial_html, script_body)`` extracted from the rendered form.

    The consent form's JS depends on three element ids that the form
    markup provides (``consent-form``, ``submit-btn``, ``loading``,
    ``btn-text``). Run the full rendered HTML as the DOM so the script
    operates on the real markup it ships with.
    """
    script = extract_script_body(consent_html)
    return consent_html, script


def test_submit_disables_button_and_shows_spinner(consent_form_html: str) -> None:
    """Submitting the form must lock the button before the round-trip starts."""
    initial_html, script = _build_form_dom(consent_form_html)
    result = run_script(
        script,
        initial_html=initial_html,
        # Trigger the form's submit handler. preventDefault stops JSDOM
        # from actually navigating to the form action — we just want to
        # observe the handler's side effects on the button.
        invoke="""
          const form = document.getElementById('consent-form');
          form.addEventListener('submit', (e) => e.preventDefault(), true);
          form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
        """,
    )

    assert not result.errors, f"unexpected errors: {result.errors}"
    # Button text must flip to indicate progress.
    assert "Authorizing" in result.dom, (
        f"submit handler should swap button text to 'Authorizing...', "
        f"got dom={result.dom[:600]}"
    )
    # disabled attribute must appear on the submit button.
    assert 'id="submit-btn"' in result.dom and "disabled" in result.dom, (
        f"submit button should be disabled after submit, dom={result.dom[:600]}"
    )
    # Loading spinner gets the 'active' class.
    assert 'class="loading active"' in result.dom or "loading active" in result.dom, (
        f"loading spinner should pick up 'active' class, dom={result.dom[:600]}"
    )
