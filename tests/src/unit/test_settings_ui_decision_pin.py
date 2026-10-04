"""The event-decisions switch around approval-PIN changes.

The switch may only be used while a PIN is set, so the page reads the PIN
status separately from the policy and locks the switch without one. These
tests drive the shipped Settings script through the JSDOM harness and check
that the switch's lock, value and status line stay correct when status reads
finish out of order and when the PIN is replaced.
"""

from __future__ import annotations

import pytest

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
# DELETE always answers as a partial removal (500 with ``pin_removed``): the
# server dropped the PIN but could not save the switch off. POST fails when
# ``postFails`` is set and leaves the PIN as it was.
# ``holdNextGet`` delays the next status read past later ones; it answers with
# the PIN as it was when the read started, which is what makes it stale. Set
# to ``'error'``, that held read fails instead. Its answer and the moment it
# settles are stamped, so a test can show the race really happened.
# ``failConfig`` makes the policy read fail (``'network'`` or ``'500'``).
PIN_SERVER_JS = """
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const json = (status, body) => ({
    ok: status < 400, status, json: async () => body,
  });
  let order = 0;
  const realFetch = window.fetch;
  let holdNextGet = false;
  let postFails = false;
  let failConfig = null;
  window.fetch = async (url, opts) => {
    if (String(url).includes('policy/config') && failConfig) {
      if (failConfig === 'network') throw new Error('network down');
      return json(500, {error: 'unreadable', policy_file_corrupt: true});
    }
    if (!String(url).includes('decision-pin')) return realFetch(url, opts);
    const method = (opts && opts.method) || 'GET';
    if (method === 'DELETE') { pinSet = false; return json(500, deleteBody); }
    if (method === 'POST') {
      if (postFails) return json(500, {error: 'not stored'});
      pinSet = true;
      return json(200, {set: true});
    }
    const answer = pinSet;
    const held = holdNextGet;
    if (held) {
      holdNextGet = false;
      await sleep(2000);
      document.body.setAttribute('data-held-answer', String(answer));
      document.body.setAttribute('data-held-order', String(++order));
    }
    if (held === 'error') throw new Error('network down');
    return json(200, {set: answer});
  };
  const toggle = document.getElementById('policy-event-decisions-toggle');
  const stamp = (name) => {
    const toast = document.querySelector('.ha-toast');
    const attrs = {
      checked: String(toggle.checked),
      disabled: String(toggle.disabled),
      order: String(++order),
      status: document.getElementById('policy-pin-status').textContent,
      'toast-error': String(!!toast && toast.classList.contains('ha-toast-error')),
      'toast-msg': toast ? toast.querySelector('.ha-toast-msg').textContent : '',
    };
    for (const [key, value] of Object.entries(attrs)) {
      document.body.setAttribute('data-' + name + '-' + key, value);
    }
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


def _held_read_landed_after(result: HarnessResult, name: str) -> bool:
    held = _probe(result, "held-order")
    fresh = _probe(result, f"{name}-order")
    assert held is not None and fresh is not None, (held, fresh)
    return int(held) > int(fresh)


# Removes the PIN (the server keeps the switch on), then sets a new one.
REPLACE_AFTER_PARTIAL_REMOVAL = """
  document.getElementById('policy-clear-pin-btn').click();
  await sleep(200);
  stamp('removed');
  document.getElementById('policy-decision-pin').value = '2468';
  document.getElementById('policy-set-pin-btn').click();
  await sleep(200);
  stamp('replaced');
"""


def test_a_stale_pin_status_does_not_lock_the_switch(settings_script: str) -> None:
    """The status read started by a policy load is not awaited. When it
    answered "no PIN" after a later read had said "set", it locked the switch
    and showed "No PIN set." until the next refresh."""
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
        """,
    )
    # The race: the newer read unlocked the switch, then the held read
    # answered "no PIN".
    assert _probe(result, "fresh-disabled") == "false"
    assert _probe(result, "held-answer") == "false"
    assert _held_read_landed_after(result, "fresh")
    assert _probe(result, "after-disabled") == "false"
    assert _probe(result, "after-status") == "A PIN is set."


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
          stamp('fresh');
          await sleep(3000);
          stamp('after');
        """,
    )
    assert _held_read_landed_after(result, "fresh")
    assert _probe(result, "after-status") == "A PIN is set."


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
        body=REPLACE_AFTER_PARTIAL_REMOVAL,
    )
    assert _probe(result, "removed-checked") == "false"
    assert _probe(result, "removed-disabled") == "true"
    assert _probe(result, "replaced-checked") == "true"
    assert _probe(result, "replaced-disabled") == "false"
    # The re-read worked, so no "could not be read" warning.
    assert _probe(result, "replaced-toast-msg") == "PIN saved."


@pytest.mark.parametrize("failure", ["network", "500"])
def test_a_failed_policy_read_does_not_report_the_saved_pin_as_failed(
    settings_script: str, failure: str
) -> None:
    """The PIN is saved before the switch's stored value is read. When that
    read fails, the user must not be told the PIN failed, but must be told
    the switch may be stale: the next global Save would write it. The cause
    goes to the console."""
    result = _run(
        settings_script,
        stored_on=True,
        pin_set=True,
        body=f"failConfig = '{failure}';\n" + REPLACE_AFTER_PARTIAL_REMOVAL,
    )
    assert _probe(result, "removed-disabled") == "true"
    toast = _probe(result, "replaced-toast-msg") or ""
    assert _probe(result, "replaced-toast-error") == "true"
    assert toast.startswith("PIN saved, but")
    assert "could not be read" in toast
    # The toast is generic; the console keeps which of the reads failed.
    warnings = [
        " ".join(entry["args"]) for entry in result.console if entry["level"] == "warn"
    ]
    assert any("event-decisions" in w for w in warnings), warnings
    assert _probe(result, "replaced-status") == "A PIN is set."
    assert _probe(result, "replaced-disabled") == "false"
    # Nothing could be read, so the switch keeps what the page showed.
    assert _probe(result, "replaced-checked") == "false"


def test_a_refused_pin_leaves_the_switch_locked(settings_script: str) -> None:
    """A PIN the server did not store must not unlock the switch or load the
    stored setting into it."""
    result = _run(
        settings_script,
        stored_on=True,
        pin_set=True,
        body="postFails = true;\n" + REPLACE_AFTER_PARTIAL_REMOVAL,
    )
    # The removal before it leaves an error toast too; the message tells
    # the two apart.
    assert _probe(result, "replaced-toast-error") == "true"
    assert _probe(result, "replaced-toast-msg") == "Set PIN failed: not stored"
    assert _probe(result, "replaced-checked") == "false"
    assert _probe(result, "replaced-disabled") == "true"


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
