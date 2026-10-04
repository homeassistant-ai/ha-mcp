"""What the policy card editor refuses or asks before it saves, and the queue
every policy write goes through (issue #2575).

Under an allow list a change that can approve more calls is never saved
without asking; a condition that can never match is not saved at all. Every
policy write reads the whole policy and writes it back, so the card saves,
the global settings save and the card reload run one at a time. These tests
drive the shipped Settings script through the JSDOM harness. What the
queue and the refusals do does not depend on the rule effect, so those
tests run one.
"""

from __future__ import annotations

import html

import pytest

from .test_settings_ui_js_behavior import _probe
from .test_settings_ui_js_behavior import settings_script as settings_script
from .test_settings_ui_policy_and_conditions import (
    FAILED_CHANGES,
    LOCK,
    RESTART,
    _run,
    _saved_rules,
    _two_conditions,
)


@pytest.mark.parametrize(
    ("effect", "change", "asks"),
    [
        # Can approve more calls: one predicate removed, or one edited (an
        # emptied value saves as "exists"); a new OR-ed condition takes the
        # same path through the form as an edit.
        ("allow", "remove", True),
        ("allow", "edit", True),
        # Only narrow what the card approves.
        ("allow", "add-and", False),
        ("allow", "remove-row", False),
        ("require_approval", "remove", False),
    ],
)
def test_allow_list_asks_before_a_change_that_approves_more(
    settings_script: str, effect: str, change: str, asks: bool
) -> None:
    """The user declines every question, so a change that asks is not
    saved. Under a require-approval list nothing asks."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          let asked = 0;
          window.confirm = () => { asked += 1; return false; };
        """
        + FAILED_CHANGES[change]
        + """
          document.body.setAttribute('data-asked', String(asked));
        """,
    )
    assert _probe(result, "asked") == ("1" if asks else "0")
    assert len(_saved_rules(result)) == (0 if asks else 1)


def test_a_condition_that_can_never_match_is_not_saved(settings_script: str) -> None:
    """``domain eq lock AND domain eq light`` never matches, so in a
    require-approval list it is a gate that never fires. The save is
    refused with the reason, not saved with a warning."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          await click('.policy-and-predicate[data-idx="0"]');
          await fill('args.domain', '"light"');
          const region = document.getElementById('ha-toast-region');
          document.body.setAttribute('data-toast', region ? region.textContent : '');
        """,
    )
    assert _saved_rules(result) == []
    assert "never matches" in html.unescape(_probe(result, "toast") or "")


# Holds the first policy save for 1.5 s and records the most policy requests
# in flight at once (PIN removal counts: it saves the policy too), and the
# policy reads from here on.
COUNT_JS = """
  const realFetch = window.fetch;
  let held = false;
  let active = 0;
  let most = 0;
  let reads = 0;
  window.fetch = async (url, opts) => {
    const counted = String(url).includes('/api/policy/config')
      || (opts && opts.method === 'DELETE');
    if (counted) { active += 1; most = Math.max(most, active); }
    if (counted && !(opts && opts.method)) reads += 1;
    try {
      if (opts && opts.method === 'PUT' && !held) { held = true; await sleep(1500); }
      return await realFetch(url, opts);
    } finally {
      if (counted) active -= 1;
    }
  };
  const card = document.querySelector('.policy-rule-card');
  card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
  await sleep(100);
"""


def test_tab_reload_waits_for_a_pending_card_save(settings_script: str) -> None:
    """A reload that read the policy before the save landed rebuilt the
    cards from the old policy; the next edit on the card then wrote the old
    conditions back."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        COUNT_JS
        + """
          activateTab('tool-security-policies');
          await sleep(2500);
          document.body.setAttribute('data-most', String(most));
          document.body.setAttribute('data-reads', String(reads));
        """,
    )
    assert _probe(result, "most") == "1"
    # The save's read, then the reload's two (the policy and the gated-tools
    # state, which loadPolicyState reads from the same endpoint).
    assert _probe(result, "reads") == "3"


def test_global_save_waits_for_a_pending_card_save(settings_script: str) -> None:
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        COUNT_JS
        + """
          document.getElementById('policy-save-global-btn').click();
          await sleep(2500);
          document.body.setAttribute('data-most', String(most));
        """,
    )
    assert _probe(result, "most") == "1"
    assert len(_saved_rules(result)) == 2


def test_pin_removal_waits_for_a_pending_card_save(settings_script: str) -> None:
    """Removing the PIN switches event decisions off by saving the policy,
    which bumps its version: sent during a card save, one of the two failed
    with a version conflict blamed on another tab."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        COUNT_JS
        + """
          document.getElementById('policy-clear-pin-btn').click();
          await sleep(2500);
          document.body.setAttribute('data-most', String(most));
        """,
    )
    assert _probe(result, "most") == "1"
    assert any(f["method"] == "DELETE" for f in result.fetches)


def test_a_save_on_another_card_waits_for_a_pending_one(settings_script: str) -> None:
    """The queue is shared by every card, not kept per tool."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        COUNT_JS
        + """
          const minutes = document.querySelector(
            '.policy-rule-card[data-tool="ha_restart"] .policy-remember-minutes');
          minutes.value = '9';
          minutes.dispatchEvent(new Event('input'));
          await sleep(2500);
          document.body.setAttribute('data-most', String(most));
        """,
    )
    assert _probe(result, "most") == "1"
    saved = _saved_rules(result)
    assert len(saved) == 2
    assert {**RESTART, "remember_minutes": 9} in saved[1]


def test_a_failed_write_does_not_stop_the_next_one(settings_script: str) -> None:
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          policyWriteOnce(async () => { throw new Error('boom'); }).catch(() => {});
          const minutes = document.querySelector('.policy-rule-card .policy-remember-minutes');
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          await sleep(800);
        """,
    )
    saved = _saved_rules(result)
    assert len(saved) == 1
    assert {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 7} in (
        saved[0]
    )


def test_remember_change_on_a_rebuilt_card_is_reported(settings_script: str) -> None:
    """The cards were rebuilt from the server before the remember save went
    out: it is not sent from the old card, and the user is told."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          const minutes = document.querySelector('.policy-rule-card .policy-remember-minutes');
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          await policyLoadConfig();
          await sleep(800);
          const region = document.getElementById('ha-toast-region');
          document.body.setAttribute('data-toast', region ? region.textContent : '');
        """,
    )
    assert _saved_rules(result) == []
    assert "reloaded before this change was saved" in (
        html.unescape(_probe(result, "toast") or "")
    )
