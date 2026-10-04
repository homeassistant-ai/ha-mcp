"""AND-ed conditions in the policy card editor (issue #2575).

A condition is one rule's ``when`` list: its predicates AND, separate
conditions OR. These tests drive the shipped Settings script through the
JSDOM harness: the card must show that grouping, and "+ AND", per-predicate
edit and per-predicate remove must change only the condition they target,
in both rule effects.
"""

from __future__ import annotations

import html
import json

import pytest

from ._js_harness import HarnessResult, run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    _assert_clean_init,
    _policy_panel_dom,
    _probe,
)
from .test_settings_ui_js_behavior import settings_script as settings_script

EFFECTS = ["require_approval", "allow"]

LOCK = {"path": "args.domain", "op": "eq", "value": "lock"}
LIGHT = {"path": "args.domain", "op": "eq", "value": "light"}
TURN_ON = {"path": "args.service", "op": "eq", "value": "turn_on"}
RESTART = {"tool_name": "ha_restart", "when": [], "remember_minutes": 5}

# ``click`` opens a form on the first card; ``fill`` completes the open form
# and saves it, ``path`` null keeping its current argument. The first card is
# ha_call_service: its rules come first in every policy below.
FILL_JS = """
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const fill = async (path, value) => {
    const card = document.querySelector('.policy-rule-card');
    if (path !== null) {
      const sel = card.querySelector('.policy-predicate-path-select');
      sel.value = '__custom__';
      sel.dispatchEvent(new Event('change'));
      const custom = card.querySelector('.policy-predicate-path-custom');
      custom.value = path;
      custom.dispatchEvent(new Event('input'));
      await sleep(100);
    }
    card.querySelector('.policy-predicate-value-control').value = value;
    card.querySelector('.policy-predicate-form-save').click();
    await sleep(200);
  };
  const click = async (selector) => {
    document.querySelector('.policy-rule-card ' + selector).click();
    await sleep(100);
  };
"""


def _policy(effect: str, tool_rules: list[dict]) -> dict:
    return {
        "rule_effect": effect,
        "wait_seconds": 60,
        "approval_ttl_minutes": 5,
        "version": 3,
        "rules": [*tool_rules, RESTART],
    }


def _two_conditions(effect: str) -> dict:
    """A single-predicate condition, then a hand-authored AND group.

    The group's lifetime is below the card maximum on purpose: a save that
    lost track of which lifetime belongs to which condition falls back to
    the maximum, so the group's 1 would come back as 60."""
    return _policy(
        effect,
        [
            {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
            {
                "tool_name": "ha_call_service",
                "when": [LIGHT, TURN_ON],
                "remember_minutes": 1,
            },
        ],
    )


def _run(
    settings_script: str,
    policy: dict,
    invoke: str,
    *,
    puts: list[dict] | None = None,
    fetches: dict | None = None,
) -> HarnessResult:
    """``puts`` sequences the responses to policy saves; every save succeeds
    without it. ``fetches`` adds or replaces routes, such as a tool schema
    in place of the default 503."""
    config = {"status": 200, "json": policy}
    if puts is not None:
        config = {"byMethod": {"GET": config, "PUT": {"responses": puts}}}
    result = run_script(
        settings_script,
        initial_html=_policy_panel_dom(),
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/policy/config": config,
            "/api/policy/tool-schema": {"status": 503, "json": {"error": "none"}},
            **(fetches or {}),
        },
        invoke="await policyLoadConfig();" + FILL_JS + invoke,
    )
    _assert_clean_init(result)
    return result


def _last_put(result: HarnessResult) -> dict:
    puts = [
        json.loads(f["body"])
        for f in result.fetches
        if f["method"] == "PUT" and "/api/policy/config" in f["url"]
    ]
    assert puts, "no policy save was sent"
    return puts[-1]


@pytest.mark.parametrize("effect", EFFECTS)
def test_card_shows_and_within_and_or_between(
    settings_script: str, effect: str
) -> None:
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          const rows = document.querySelectorAll(
            '.policy-rule-card[data-tool="ha_call_service"] .policy-predicate-row');
          const count = (r, sel) => String(r.querySelectorAll(sel).length);
          rows.forEach((r, i) => {
            document.body.setAttribute('data-or' + i, count(r, '.policy-or-join'));
            document.body.setAttribute('data-and' + i, count(r, '.policy-and-join'));
            document.body.setAttribute('data-edit' + i, count(r, '.policy-edit-predicate'));
            document.body.setAttribute('data-part' + i, count(r, '.policy-remove-part'));
            document.body.setAttribute('data-addand' + i, count(r, '.policy-and-predicate'));
          });
          document.body.setAttribute('data-title',
            rows[0].querySelector('.policy-and-predicate').title);
        """,
    )
    # Row 0: one predicate, no OR lead, no AND join, no per-predicate remove.
    assert [_probe(result, f"{k}0") for k in ("or", "and", "edit", "part")] == [
        "0",
        "0",
        "1",
        "0",
    ]
    # Row 1: led by OR, its two predicates joined by AND, each editable and
    # removable on its own.
    assert [_probe(result, f"{k}1") for k in ("or", "and", "edit", "part")] == [
        "1",
        "1",
        "2",
        "2",
    ]
    assert _probe(result, "addand0") == "1"
    assert _probe(result, "addand1") == "1"
    # The "+ AND" tooltip states the direction the addition moves in this mode.
    expected = "approves fewer calls" if effect == "allow" else "matches fewer calls"
    assert expected in (_probe(result, "title") or "")


@pytest.mark.parametrize("effect", EFFECTS)
def test_add_and_extends_only_that_condition(settings_script: str, effect: str) -> None:
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          await click('.policy-and-predicate[data-idx="0"]');
          document.body.setAttribute('data-hint',
            document.querySelector('.policy-predicate-form-hint').textContent);
          await fill('args.entity_id', '"lock.front_door"');
        """,
    )
    hint = html.unescape(_probe(result, "hint") or "")
    assert hint.startswith("Must also match: args.domain ")
    assert hint.endswith('"lock"')
    body = _last_put(result)
    assert body["rules"] == [
        {
            "tool_name": "ha_call_service",
            "when": [
                LOCK,
                {"path": "args.entity_id", "op": "eq", "value": "lock.front_door"},
            ],
            "remember_minutes": 60,
        },
        {
            "tool_name": "ha_call_service",
            "when": [LIGHT, TURN_ON],
            "remember_minutes": 1,
        },
        RESTART,
    ]
    assert body["rule_effect"] == effect


@pytest.mark.parametrize("effect", EFFECTS)
def test_edit_changes_one_predicate_of_a_group(
    settings_script: str, effect: str
) -> None:
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          await click('.policy-edit-predicate[data-idx="1"][data-pred="1"]');
          document.body.setAttribute('data-path',
            document.querySelector('.policy-predicate-path-custom').value);
          document.body.setAttribute('data-hint',
            document.querySelector('.policy-predicate-form-hint').textContent);
          await fill(null, '"turn_off"');
        """,
    )
    # The form opens on the clicked predicate and names the rest of its group.
    assert _probe(result, "path") == "args.service"
    hint = html.unescape(_probe(result, "hint") or "")
    assert hint.startswith("Must also match: args.domain ")
    assert hint.endswith('"light"')
    assert "args.service" not in hint
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {
            "tool_name": "ha_call_service",
            "when": [LIGHT, {**TURN_ON, "value": "turn_off"}],
            "remember_minutes": 1,
        },
        RESTART,
    ]


@pytest.mark.parametrize("effect", EFFECTS)
def test_removing_one_predicate_keeps_the_rest_of_the_group(
    settings_script: str, effect: str
) -> None:
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          await click('.policy-remove-part[data-idx="1"][data-pred="0"]');
          await sleep(200);
        """,
    )
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 1},
        RESTART,
    ]


def _saved_rules(result: HarnessResult) -> list[list[dict]]:
    return [
        json.loads(f["body"])["rules"]
        for f in result.fetches
        if f["method"] == "PUT" and "/api/policy/config" in f["url"]
    ]


# A second click on the same card before the first save lands. The same
# Remove again would empty the group, saving ``when: []``, which under an
# allow list approves every call without the approve-all confirmation; a
# different edit would be built on the rule before the first removal.
SECOND_CLICKS = {
    "same-remove": '.policy-remove-part[data-idx="1"][data-pred="0"]',
    "other-row": '.policy-remove-predicate[data-idx="0"]',
}


@pytest.mark.parametrize("effect", EFFECTS)
@pytest.mark.parametrize("second", SECOND_CLICKS)
def test_click_during_a_pending_save_is_ignored(
    settings_script: str, effect: str, second: str
) -> None:
    result = _run(
        settings_script,
        _two_conditions(effect),
        f"""
          const card = document.querySelector('.policy-rule-card');
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          card.querySelector('{SECOND_CLICKS[second]}').click();
          const region = document.getElementById('ha-toast-region');
          document.body.setAttribute('data-toast', region ? region.textContent : '');
          await sleep(300);
          document.body.setAttribute('data-codes', Array.from(document.querySelectorAll(
            '.policy-rule-card[data-tool="ha_call_service"] .policy-predicate-row code'
          )).map(c => c.textContent).join('|'));
        """,
    )
    assert _saved_rules(result) == [
        [
            {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
            {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 1},
            RESTART,
        ]
    ]
    codes = html.unescape(_probe(result, "codes") or "").split("|")
    assert len(codes) == 2
    assert "lock" in codes[0]
    assert "turn_on" in codes[1]
    # The ignored click says so rather than vanishing.
    assert "still being saved" in html.unescape(_probe(result, "toast") or "")


@pytest.mark.parametrize("effect", EFFECTS)
def test_removing_a_row_keeps_each_lifetime_with_its_condition(
    settings_script: str, effect: str
) -> None:
    """Removing a row drops its lifetime too, and the card keeps the result:
    the group keeps its 1, not the removed row's 60, on this save and the
    next one."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          await click('.policy-remove-predicate[data-idx="0"]');
          await sleep(200);
          await click('.policy-remove-part[data-idx="0"][data-pred="1"]');
          await sleep(200);
        """,
    )
    assert _saved_rules(result) == [
        [
            {
                "tool_name": "ha_call_service",
                "when": [LIGHT, TURN_ON],
                "remember_minutes": 1,
            },
            RESTART,
        ],
        [
            {"tool_name": "ha_call_service", "when": [LIGHT], "remember_minutes": 1},
            RESTART,
        ],
    ]


@pytest.mark.parametrize("effect", EFFECTS)
def test_remember_change_waits_for_a_pending_condition_save(
    settings_script: str, effect: str
) -> None:
    """The remember-minutes save sends the whole rule. Sent while a condition
    save is pending, it carries the old conditions, and whichever lands last
    drops the other change. The first save is held past the debounce."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          const realFetch = window.fetch;
          let held = false;
          window.fetch = async (url, opts) => {
            if (opts && opts.method === 'PUT' && !held) { held = true; await sleep(2000); }
            return realFetch(url, opts);
          };
          const card = document.querySelector('.policy-rule-card');
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          const minutes = card.querySelector('.policy-remember-minutes');
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          await sleep(3000);
        """,
    )
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 7},
        {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 7},
        RESTART,
    ]


@pytest.mark.parametrize("effect", EFFECTS)
def test_condition_click_during_a_pending_remember_save_is_ignored(
    settings_script: str, effect: str
) -> None:
    """The other order: a condition save sent while the remember-minutes save
    is pending would be undone if the remember save, which carries the old
    conditions, landed last. The remember save is held."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          const realFetch = window.fetch;
          let held = false;
          window.fetch = async (url, opts) => {
            if (opts && opts.method === 'PUT' && !held) { held = true; await sleep(2000); }
            return realFetch(url, opts);
          };
          const card = document.querySelector('.policy-rule-card');
          const minutes = card.querySelector('.policy-remember-minutes');
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          await sleep(700);
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          await sleep(3000);
        """,
    )
    assert _saved_rules(result) == [
        [
            {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 7},
            {
                "tool_name": "ha_call_service",
                "when": [LIGHT, TURN_ON],
                "remember_minutes": 7,
            },
            RESTART,
        ]
    ]


@pytest.mark.parametrize("effect", EFFECTS)
def test_remember_changes_queued_behind_a_save_go_out_one_at_a_time(
    settings_script: str, effect: str
) -> None:
    """Two remember-minutes changes that both wait for the same pending save
    must not then save side by side: the card would allow condition edits
    while the second one is still pending. Counts overlapping policy
    requests; the first save is held."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          const realFetch = window.fetch;
          let held = false;
          let active = 0;
          let most = 0;
          window.fetch = async (url, opts) => {
            const counted = String(url).includes('/api/policy/config');
            if (counted) { active += 1; most = Math.max(most, active); }
            try {
              if (opts && opts.method === 'PUT' && !held) { held = true; await sleep(2000); }
              return await realFetch(url, opts);
            } finally {
              if (counted) active -= 1;
            }
          };
          const card = document.querySelector('.policy-rule-card');
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          const minutes = card.querySelector('.policy-remember-minutes');
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          await sleep(600);
          minutes.value = '8';
          minutes.dispatchEvent(new Event('input'));
          await sleep(3000);
          document.body.setAttribute('data-most', String(most));
        """,
    )
    assert _probe(result, "most") == "1"
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 8},
        {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 8},
        RESTART,
    ]


# Holds the policy save numbered ``hold`` (1-based) for 1.5 s.
HOLD_PUT_JS = """
  const realFetch = window.fetch;
  let putCount = 0;
  window.fetch = async (url, opts) => {
    if (opts && opts.method === 'PUT' && ++putCount === %d) await sleep(1500);
    return realFetch(url, opts);
  };
  const card = document.querySelector('.policy-rule-card');
  const minutes = card.querySelector('.policy-remember-minutes');
"""


@pytest.mark.parametrize("effect", EFFECTS)
def test_remember_save_from_a_replaced_card_still_blocks_condition_edits(
    settings_script: str, effect: str
) -> None:
    """A condition save re-renders the card, but the remember save queued on
    the card it replaced is still a pending write: a condition click on the
    new card meanwhile is ignored. Saves 2 is the remember save, held."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        HOLD_PUT_JS % 2
        + """
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          await sleep(800);
          document.querySelector('.policy-rule-card .policy-remove-predicate[data-idx="0"]').click();
          await sleep(3000);
        """,
    )
    after_removal = [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 7},
        {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 7},
        RESTART,
    ]
    assert _saved_rules(result) == [after_removal, after_removal]


@pytest.mark.parametrize("effect", EFFECTS)
def test_removing_the_card_drops_its_pending_remember_save(
    settings_script: str, effect: str
) -> None:
    """A remember save still waiting to go out must not put the tool's rules
    back after "Remove from policy"."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        HOLD_PUT_JS % 0
        + """
          window.confirm = () => true;
          minutes.value = '7';
          minutes.dispatchEvent(new Event('input'));
          card.querySelector('.policy-rule-remove').click();
          await sleep(2000);
        """,
    )
    assert _saved_rules(result) == [[RESTART]]


@pytest.mark.parametrize("effect", EFFECTS)
def test_removing_the_card_waits_for_a_pending_condition_save(
    settings_script: str, effect: str
) -> None:
    """ "Remove from policy" during a condition save goes out after it: sent
    alongside, the condition save could land last and put the rules back."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        HOLD_PUT_JS % 1
        + """
          window.confirm = () => true;
          card.querySelector('.policy-remove-part[data-idx="1"][data-pred="0"]').click();
          card.querySelector('.policy-rule-remove').click();
          await sleep(3000);
        """,
    )
    assert _last_put(result)["rules"] == [RESTART]


FAILED_CHANGES = {
    "remove": """
      await click('.policy-remove-part[data-idx="1"][data-pred="0"]');
      await sleep(200);
    """,
    "add-and": """
      await click('.policy-and-predicate[data-idx="0"]');
      await fill('args.entity_id', '"lock.front_door"');
    """,
    "edit": """
      await click('.policy-edit-predicate[data-idx="0"][data-pred="0"]');
      await fill(null, '"garage"');
    """,
    "new-condition": """
      await click('.policy-add-predicate');
      await fill('args.domain', '"switch"');
    """,
    "remove-row": """
      await click('.policy-remove-predicate[data-idx="0"]');
      await sleep(200);
    """,
}


@pytest.mark.parametrize("effect", EFFECTS)
@pytest.mark.parametrize("change", FAILED_CHANGES)
def test_failed_save_is_not_carried_into_the_next_one(
    settings_script: str, effect: str, change: str
) -> None:
    """When a save fails and the recovery reload fails too, the card keeps
    the rule it showed, so the next save must not include the failed change."""
    result = _run(
        settings_script,
        _two_conditions(effect),
        """
          policyLoadConfig = async () => { throw new Error('offline'); };
        """
        + FAILED_CHANGES[change]
        + """
          await click('.policy-remove-part[data-idx="1"][data-pred="1"]');
          await sleep(200);
        """,
        puts=[{"status": 500, "body": "boom"}, {"status": 200, "json": {}}],
    )
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {"tool_name": "ha_call_service", "when": [LIGHT], "remember_minutes": 1},
        RESTART,
    ]


@pytest.mark.parametrize("effect", EFFECTS)
def test_editing_a_value_keeps_the_operator_and_the_position(
    settings_script: str, effect: str
) -> None:
    """Editing only the value of a non-equals predicate must not turn it into
    equals, and must land on the clicked predicate rather than its mirror
    position in another condition (row 1, predicate 0 vs row 0, predicate 1)."""
    contains = {"path": "args.entity_id", "op": "contains", "value": "lock."}
    policy = _policy(
        effect,
        [
            {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
            {
                "tool_name": "ha_call_service",
                "when": [contains, TURN_ON],
                "remember_minutes": 1,
            },
        ],
    )
    result = _run(
        settings_script,
        policy,
        """
          await click('.policy-edit-predicate[data-idx="1"][data-pred="0"]');
          await fill(null, '"garage"');
        """,
    )
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {
            "tool_name": "ha_call_service",
            "when": [{**contains, "value": "garage"}, TURN_ON],
            "remember_minutes": 1,
        },
        RESTART,
    ]


def test_overlapping_edit_openings_fill_the_form_for_the_last_click(
    settings_script: str,
) -> None:
    """Two edit clicks before the first tool-schema fetch returns: the later
    click is the save target, so the form must show its predicate even when
    the earlier fetch lands last. The first fetch is held past the second.
    Opening the form does not depend on the rule effect, so this runs one
    effect rather than both."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          const realFetch = window.fetch;
          let held = false;
          window.fetch = async (url, opts) => {
            if (String(url).includes('tool-schema') && !held) { held = true; await sleep(2000); }
            return realFetch(url, opts);
          };
          const card = document.querySelector('.policy-rule-card');
          card.querySelector('.policy-edit-predicate[data-idx="1"][data-pred="0"]').click();
          card.querySelector('.policy-edit-predicate[data-idx="1"][data-pred="1"]').click();
          await sleep(3000);
          document.body.setAttribute('data-held', String(held));
          await fill(null, '"turn_off"');
        """,
    )
    # The race needs the two openings to overlap on the schema fetch.
    assert _probe(result, "held") == "true"
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {
            "tool_name": "ha_call_service",
            "when": [LIGHT, {**TURN_ON, "value": "turn_off"}],
            "remember_minutes": 1,
        },
        RESTART,
    ]


def test_save_while_value_choices_load_keeps_the_predicate(
    settings_script: str,
) -> None:
    """Save while the value choices are still loading has no value to read.
    Taken as a blank value it saved ``args.domain exists`` in place of
    ``args.domain eq light``, which under an allow list approves every
    domain. Nothing may be saved, and the form says why."""
    schema = {
        "paths": [{"path": "args.domain", "type": "str"}],
        "value_sources": {"args.domain": "domains"},
    }
    result = _run(
        settings_script,
        _two_conditions("allow"),
        """
          const realFetch = window.fetch;
          window.fetch = async (url, opts) => {
            if (String(url).includes('value-source')) await sleep(2000);
            return realFetch(url, opts);
          };
          await click('.policy-edit-predicate[data-idx="1"][data-pred="0"]');
          const card = document.querySelector('.policy-rule-card');
          document.body.setAttribute('data-loading',
            String(!card.querySelector('.policy-predicate-value-control')));
          card.querySelector('.policy-predicate-form-save').click();
          await sleep(100);
          document.body.setAttribute('data-error',
            card.querySelector('.policy-predicate-form-error').textContent);
          await sleep(3000);
        """,
        fetches={
            "/api/policy/tool-schema": {"status": 200, "json": schema},
            "/api/policy/value-source": {
                "status": 200,
                "json": {"values": ["light", "lock"]},
            },
        },
    )
    # Save was clicked while the choices were loading.
    assert _probe(result, "loading") == "true"
    assert _saved_rules(result) == []
    assert _probe(result, "error") == "still loading, try again in a moment"


def test_predicate_buttons_name_their_predicate(settings_script: str) -> None:
    """In a group every edit and Remove button shows the same label, so a
    screen reader or voice control could not tell which predicate one acts
    on. Each name carries its predicate, after the visible label."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          const buttons = document.querySelectorAll(
            '.policy-rule-card .policy-edit-predicate[data-idx="1"],' +
            ' .policy-rule-card .policy-remove-part[data-idx="1"]');
          document.body.setAttribute('data-names', JSON.stringify(Array.from(buttons).map(b => ({
            pred: b.dataset.pred,
            name: b.textContent,
            visible: Array.from(b.childNodes)
              .filter(n => !(n.classList && n.classList.contains('visually-hidden')))
              .map(n => n.textContent).join(''),
          }))));
        """,
    )
    buttons = json.loads(html.unescape(_probe(result, "names") or "[]"))
    assert len(buttons) == 4
    for b in buttons:
        own, other = (LIGHT, TURN_ON) if b["pred"] == "0" else (TURN_ON, LIGHT)
        assert b["visible"].strip()
        assert b["name"].startswith(b["visible"])
        assert own["value"] in b["name"]
        assert other["value"] not in b["name"]
        assert own["value"] not in b["visible"]


def test_new_condition_form_drops_the_previous_group_hint(
    settings_script: str,
) -> None:
    """After "+ AND" on a group, "+ Add condition" opens an OR-ed condition;
    a leftover "Must also match" would describe the wrong grouping."""
    result = _run(
        settings_script,
        _two_conditions("require_approval"),
        """
          await click('.policy-and-predicate[data-idx="1"]');
          document.querySelector('.policy-predicate-form-cancel').click();
          await click('.policy-add-predicate');
          const hint = document.querySelector('.policy-predicate-form-hint');
          document.body.setAttribute('data-hint', hint.textContent);
          document.body.setAttribute('data-hidden', String(hint.style.display === 'none'));
        """,
    )
    assert _probe(result, "hint") == ""
    assert _probe(result, "hidden") == "true"


def test_predicate_values_render_as_text(settings_script: str) -> None:
    """A predicate value from tool_policy.json must not become markup on the
    card, in a group's parts or in the never-matches warning."""
    payload = "<img src=x onerror=alert(1)>"
    policy = _policy(
        "require_approval",
        [
            {
                "tool_name": "ha_call_service",
                "when": [
                    {**LIGHT, "value": payload},
                    {**LOCK, "path": "args." + payload},
                ],
                "remember_minutes": 0,
            },
            {
                "tool_name": "ha_call_service",
                "when": [
                    {**LIGHT, "path": "args." + payload},
                    {**LOCK, "path": "args." + payload},
                ],
                "remember_minutes": 0,
            },
        ],
    )
    result = _run(
        settings_script,
        policy,
        """
          const card = document.querySelector('.policy-rule-card[data-tool="ha_call_service"]');
          document.body.setAttribute('data-imgs', String(card.querySelectorAll('img').length));
          document.body.setAttribute('data-warned',
            String(card.querySelectorAll('.policy-condition-warning').length));
        """,
    )
    assert _probe(result, "warned") == "1"
    assert _probe(result, "imgs") == "0"


@pytest.mark.parametrize("effect", EFFECTS)
def test_an_always_row_offers_no_and(settings_script: str, effect: str) -> None:
    """ "+ AND" on "(always)" would turn an unconditional rule into a
    conditional one without saying so; in a require-approval list that stops
    gating every call the new predicate does not match. "+ Add condition" is still there."""
    always = _policy(
        effect, [{"tool_name": "ha_call_service", "when": [], "remember_minutes": 0}]
    )
    result = _run(
        settings_script,
        always,
        """
          const card = document.querySelector('.policy-rule-card[data-tool="ha_call_service"]');
          document.body.setAttribute('data-and',
            String(card.querySelectorAll('.policy-and-predicate').length));
          document.body.setAttribute('data-add',
            String(card.querySelectorAll('.policy-add-predicate').length));
        """,
    )
    assert _probe(result, "and") == "0"
    assert _probe(result, "add") == "1"


@pytest.mark.parametrize("effect", EFFECTS)
def test_group_built_in_the_ui_reloads_as_one_condition(
    settings_script: str, effect: str
) -> None:
    one = _policy(
        effect,
        [{"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 0}],
    )
    built = _run(
        settings_script,
        one,
        """
          await click('.policy-add-predicate');
          await fill('args.domain', '"light"');
          await click('.policy-and-predicate[data-idx="1"]');
          await fill('args.service', '"turn_on"');
        """,
    )
    saved = _last_put(built)
    assert saved["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 0},
        {
            "tool_name": "ha_call_service",
            "when": [LIGHT, TURN_ON],
            "remember_minutes": 0,
        },
        RESTART,
    ]
    # Load what was saved: the group comes back as one condition row.
    reloaded = _run(
        settings_script,
        saved,
        """
          const rows = document.querySelectorAll(
            '.policy-rule-card[data-tool="ha_call_service"] .policy-predicate-row');
          document.body.setAttribute('data-rows', String(rows.length));
          document.body.setAttribute('data-codes',
            String(rows[1].querySelectorAll('code').length));
        """,
    )
    assert _probe(reloaded, "rows") == "2"
    assert _probe(reloaded, "codes") == "2"


@pytest.mark.parametrize(
    ("when", "flagged"),
    [
        pytest.param([LIGHT, LOCK], True, id="one-argument-two-values"),
        pytest.param([LIGHT, TURN_ON], False, id="two-arguments"),
        pytest.param([LIGHT, {**LIGHT, "value": "Light"}], False, id="case-only"),
        pytest.param(
            [{**LIGHT, "path": "args.*"}, {**LOCK, "path": "args.*"}],
            False,
            id="wildcard-path",
        ),
        pytest.param([LIGHT, {**LOCK, "op": "neq"}], False, id="not-both-equals"),
        pytest.param([LIGHT, {**LIGHT, "op": "neq"}], True, id="equals-and-not"),
        pytest.param(
            [LIGHT, {**LIGHT, "op": "not_in", "value": ["lock", "Light"]}],
            True,
            id="equals-and-not-one-of",
        ),
        pytest.param(
            [LIGHT, {**LIGHT, "op": "in", "value": ["lock", "switch"]}],
            True,
            id="equals-and-one-of-without-it",
        ),
        pytest.param(
            [LIGHT, {**LIGHT, "op": "in", "value": ["lock", "light"]}],
            False,
            id="equals-and-one-of-with-it",
        ),
        pytest.param(
            [{**LIGHT, "path": "domain"}, LOCK], True, id="args-prefix-optional"
        ),
        pytest.param(
            [{**LIGHT, "value": True}, {**LOCK, "value": 1}],
            False,
            id="non-string-values",
        ),
        pytest.param(
            [{"op": "eq", "value": "light"}, {"op": "eq", "value": "lock"}],
            False,
            id="no-path",
        ),
    ],
)
def test_condition_that_can_never_match_is_flagged(
    settings_script: str, when: list[dict], flagged: bool
) -> None:
    """A group that saves fine but can never match any call would
    otherwise fail silently: the row carries a warning naming the argument."""
    policy = _policy(
        "require_approval",
        [{"tool_name": "ha_call_service", "when": when, "remember_minutes": 0}],
    )
    result = _run(
        settings_script,
        policy,
        """
          const w = document.querySelector(
            '.policy-rule-card[data-tool="ha_call_service"] .policy-condition-warning');
          document.body.setAttribute('data-warning', w ? w.textContent : '');
          document.body.setAttribute('data-role', w ? w.getAttribute('role') : '');
        """,
    )
    warning = html.unescape(_probe(result, "warning") or "")
    if flagged:
        assert "No single value of" in warning
        assert "domain" in warning
        # The warning appears when a save re-renders the card; only an alert
        # is announced by screen readers when it is inserted.
        assert _probe(result, "role") == "alert"
    else:
        assert warning == ""
