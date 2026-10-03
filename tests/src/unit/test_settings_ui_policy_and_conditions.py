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

from ._js_harness import HarnessResult, extract_script_body, run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    _assert_clean_init,
    _policy_panel_dom,
    _probe,
)

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


@pytest.fixture(scope="module")
def settings_script() -> str:
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)


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


def _run(settings_script: str, policy: dict, invoke: str) -> HarnessResult:
    result = run_script(
        settings_script,
        initial_html=_policy_panel_dom(),
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/policy/config": {"status": 200, "json": policy},
            "/api/policy/tool-schema": {"status": 503, "json": {"error": "none"}},
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
          let asked = 0;
          window.confirm = () => { asked += 1; return true; };
          await click('.policy-remove-part[data-idx="1"][data-pred="0"]');
          await sleep(200);
          document.body.setAttribute('data-asked', String(asked));
        """,
    )
    assert _probe(result, "asked") == "0"
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 60},
        {"tool_name": "ha_call_service", "when": [TURN_ON], "remember_minutes": 1},
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
def test_and_on_an_always_row_narrows_it(settings_script: str, effect: str) -> None:
    always = _policy(
        effect, [{"tool_name": "ha_call_service", "when": [], "remember_minutes": 0}]
    )
    result = _run(
        settings_script,
        always,
        """
          await click('.policy-and-predicate[data-idx="0"]');
          await fill('args.domain', '"lock"');
        """,
    )
    assert _last_put(result)["rules"] == [
        {"tool_name": "ha_call_service", "when": [LOCK], "remember_minutes": 0},
        RESTART,
    ]


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
        """,
    )
    warning = html.unescape(_probe(result, "warning") or "")
    if flagged:
        assert "cannot equal two different values" in warning
        assert "domain" in warning
    else:
        assert warning == ""
