"""The event-decisions switch around approval-PIN changes.

The switch may only be used while a PIN is set, so the page reads the PIN
status separately from the policy and locks the switch without one. These
tests drive the shipped Settings script through the JSDOM harness and check
that the switch shows what the server holds after the PIN changes.
"""

from __future__ import annotations

from ._js_harness import HarnessResult, run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    _assert_clean_init,
    _policy_panel_dom,
    _probe,
)
from .test_settings_ui_js_behavior import settings_script as settings_script

# The decision-PIN endpoint answers from ``pinSet``, which DELETE and POST
# change, so each test states the server's PIN without counting init GETs.
# ``holdNextGet`` delays the next status answer past later requests; set to
# ``'error'``, that held request then fails instead.
PIN_SERVER_JS = """
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const json = (status, body) => ({
    ok: status < 400, status, json: async () => body,
  });
  const realFetch = window.fetch;
  let holdNextGet = false;
  window.fetch = async (url, opts) => {
    if (!String(url).includes('decision-pin')) return realFetch(url, opts);
    const method = (opts && opts.method) || 'GET';
    if (method === 'DELETE') { pinSet = false; return json(500, deleteBody); }
    if (method === 'POST') { pinSet = true; return json(200, {set: true}); }
    const answer = pinSet;
    const held = holdNextGet;
    if (held) { holdNextGet = false; await sleep(2000); }
    if (held === 'error') throw new Error('network down');
    return json(200, {set: answer});
  };
  const toggle = document.getElementById('policy-event-decisions-toggle');
  const stamp = (name) => {
    document.body.setAttribute('data-' + name + '-checked', String(toggle.checked));
    document.body.setAttribute('data-' + name + '-disabled', String(toggle.disabled));
  };
"""


def _run(
    settings_script: str, *, stored_on: bool, pin_set: bool, body: str
) -> HarnessResult:
    fetches = {
        **DEFAULT_FETCHES,
        "/api/policy/config": {
            "status": 200,
            "json": {
                "rule_effect": "require_approval",
                "wait_seconds": 60,
                "approval_ttl_minutes": 5,
                "event_decisions_enabled": stored_on,
                "version": 3,
                "rules": [],
            },
        },
    }
    result = run_script(
        settings_script,
        initial_html=_policy_panel_dom(),
        fetch_map=fetches,
        invoke=f"""
          await new Promise(r => setTimeout(r, 250));
          window.confirm = () => true;
          let pinSet = {str(pin_set).lower()};
          const deleteBody = {{error: 'save failed', pin_removed: true}};
          {PIN_SERVER_JS}
          await window.policyLoadConfig();
          await sleep(100);
          {body}
        """,
    )
    _assert_clean_init(result)
    return result


def test_a_stale_pin_status_does_not_lock_the_switch(settings_script: str) -> None:
    """The status read started by a policy load is not awaited. Answering
    "no PIN" after a later read said "set", it locked the switch until the
    next refresh."""
    result = _run(
        settings_script,
        stored_on=False,
        pin_set=False,
        body="""
          holdNextGet = true;
          window.policyLoadConfig();
          await sleep(100);
          pinSet = true;
          await window.policyRefreshPinStatus();
          stamp('fresh');
          await sleep(3000);
          stamp('after');
          document.body.setAttribute('data-status',
            document.getElementById('policy-pin-status').textContent);
        """,
    )
    # The race needs the newer read to have unlocked the switch first.
    assert _probe(result, "fresh-disabled") == "false"
    assert _probe(result, "after-disabled") == "false"
    assert _probe(result, "status") == "A PIN is set."


def test_a_stale_failed_pin_status_does_not_overwrite_a_newer_one(
    settings_script: str,
) -> None:
    """A stale read that fails must not replace the newer status line with
    "could not read"."""
    result = _run(
        settings_script,
        stored_on=False,
        pin_set=True,
        body="""
          holdNextGet = 'error';
          window.policyLoadConfig();
          await sleep(100);
          await window.policyRefreshPinStatus();
          await sleep(3000);
          document.body.setAttribute('data-status',
            document.getElementById('policy-pin-status').textContent);
        """,
    )
    assert _probe(result, "status") == "A PIN is set."


def test_a_new_pin_shows_the_stored_switch_after_a_partial_removal(
    settings_script: str,
) -> None:
    """The server removed the PIN but could not save the switch off, so the
    stored setting stays on while the page unticks the switch. Once a new
    PIN makes the switch usable, it must show the stored value again."""
    result = _run(
        settings_script,
        stored_on=True,
        pin_set=True,
        body="""
          document.getElementById('policy-clear-pin-btn').click();
          await sleep(200);
          stamp('removed');
          document.getElementById('policy-decision-pin').value = '2468';
          document.getElementById('policy-set-pin-btn').click();
          await sleep(200);
          stamp('replaced');
        """,
    )
    assert _probe(result, "removed-checked") == "false"
    assert _probe(result, "removed-disabled") == "true"
    assert _probe(result, "replaced-checked") == "true"
    assert _probe(result, "replaced-disabled") == "false"


def test_replacing_a_pin_keeps_an_unsaved_tick(settings_script: str) -> None:
    """An unlocked switch may hold an edit not saved yet; replacing the PIN
    must not reset it to the stored value."""
    result = _run(
        settings_script,
        stored_on=False,
        pin_set=True,
        body="""
          toggle.checked = true;
          stamp('ticked');
          document.getElementById('policy-decision-pin').value = '2468';
          document.getElementById('policy-set-pin-btn').click();
          await sleep(200);
          stamp('replaced');
        """,
    )
    assert _probe(result, "ticked-disabled") == "false"
    assert _probe(result, "replaced-checked") == "true"
