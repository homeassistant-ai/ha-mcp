"""Policies and card edits shared by the policy card editor's JS behaviour tests."""

from __future__ import annotations

import json

from ._astro_setup_wizard import _assert_clean_init
from ._js_harness import HarnessResult, run_script
from ._settings_ui_dom import DEFAULT_FETCHES
from ._settings_ui_js_helpers import policy_panel_dom

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


def make_policy(effect: str, tool_rules: list[dict]) -> dict:
    return {
        "rule_effect": effect,
        "wait_seconds": 60,
        "approval_ttl_minutes": 5,
        "version": 3,
        "rules": [*tool_rules, RESTART],
    }


def two_conditions(effect: str) -> dict:
    """A single-predicate condition, then a hand-authored AND group.

    The group's lifetime is below the card maximum on purpose: a save that
    lost track of which lifetime belongs to which condition falls back to
    the maximum, so the group's 1 would come back as 60."""
    return make_policy(
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


def run(
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
        initial_html=policy_panel_dom(),
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


def saved_rules(result: HarnessResult) -> list[list[dict]]:
    return [
        json.loads(f["body"])["rules"]
        for f in result.fetches
        if f["method"] == "PUT" and "/api/policy/config" in f["url"]
    ]
