"""JSDOM coverage for the redacted-app-option-keys row (#2843).

The row comes from the advanced cache (section ``redact_keys``) but renders
nested under the Redact Secrets toggle, dimmed and read-only while that
toggle is off, since the list does nothing then.
"""

from __future__ import annotations

import re

import pytest

from ._js_harness import HarnessResult, run_script
from .test_backup_restore_consumers import settings_script as settings_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES, MIN_DOM, _assert_clean_init

READ_INPUT = """
  const input = document.querySelector('[name="adv:redact_app_option_keys"]');
  console.log('redact-keys', input.tagName, input.value, input.disabled);
"""


def _payloads(redact_on: bool) -> dict[str, dict]:
    return {
        **DEFAULT_FETCHES,
        "/api/settings/features": {
            "status": 200,
            "json": {
                "flags": {
                    "redact_secrets": {
                        "value": redact_on,
                        "origin": "default",
                        "editable": True,
                        "type": "bool",
                        "env_var": "REDACT_SECRETS",
                    },
                },
                "beta_sub_flags": [],
            },
        },
        "/api/settings/advanced": {
            "status": 200,
            "json": {
                "fields": [
                    {
                        "field": "redact_app_option_keys",
                        "env_var": "REDACT_APP_OPTION_KEYS",
                        "value": "backup_password",
                        "type": "str",
                        "section": "redact_keys",
                        "origin": "default",
                        "editable": True,
                    }
                ]
            },
        },
    }


def _row(dom: str) -> str:
    rows = re.findall(r'<div[^>]*class="[^"]*redact-sub[^"]*"[^>]*>', dom)
    assert len(rows) == 1, f"expected one redact-sub row; tail: {dom[-2000:]}"
    return rows[0]


def _logged_input(result: HarnessResult) -> list[str]:
    return next(e["args"] for e in result.console if e["args"][0] == "redact-keys")


@pytest.mark.parametrize("redact_on", [True, False])
def test_key_list_is_editable_only_while_redact_secrets_is_on(
    settings_script: str, redact_on: bool
) -> None:
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        fetch_map=_payloads(redact_on),
        invoke="await new Promise(r => setTimeout(r, 300));" + READ_INPUT,
    )
    _assert_clean_init(result)

    assert ("dimmed" in _row(result.dom)) is not redact_on
    assert _logged_input(result) == [
        "redact-keys",
        "TEXTAREA",
        "backup_password",
        "false" if redact_on else "true",
    ]


def test_turning_redact_secrets_on_unlocks_the_key_list(settings_script: str) -> None:
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        fetch_map=_payloads(redact_on=False),
        invoke="""
          await new Promise(r => setTimeout(r, 300));
          const toggle = document.querySelector('[name="feature:redact_secrets"]');
          toggle.checked = true;
          toggle.dispatchEvent(new Event('change'));
          await new Promise(r => setTimeout(r, 300));
        """
        + READ_INPUT,
    )
    _assert_clean_init(result)

    assert "dimmed" not in _row(result.dom)
    assert _logged_input(result)[3] == "false"
