"""JSDOM coverage for the redacted-app-option-keys row (#2843).

The row comes from the advanced cache (section ``redact_keys``) but renders
nested under the Redact Secrets toggle, dimmed and read-only while that
toggle is off, since the list does nothing then.
"""

from __future__ import annotations

import re

import pytest

from ._js_harness import HarnessResult, extract_script_body, run_script

MODAL_SCAFFOLD = (
    '<div id="modalBackdrop"><div class="modal" role="dialog" tabindex="-1">'
    '<div class="modal-header"><span class="modal-title" id="modalTitle"></span>'
    '<button class="modal-close" id="modalClose">x</button></div>'
    '<div class="modal-body" id="modalBody"></div></div></div>'
)
CHILD_OF_SCAFFOLD = {"modalBody", "modalTitle", "modalClose", "restartNoticeText"}
BUTTON_IDS = {
    "restartBtn",
    "stopSidecarBtn",
    "backupConfigSave",
    "backupRefresh",
    "backupBulkDelete",
}
TABLE_BODY_IDS = {"backupList", "featuresBody"}

DEFAULT_FETCHES: dict[str, dict] = {
    "/api/settings/features": {"status": 200, "json": {"features": [], "values": {}}},
    "/api/settings/tools": {
        "status": 200,
        "json": {"tools": [], "states": {}, "addon_mode": True},
    },
    "/api/settings/info": {"status": 200, "json": {"instance_id": "baseline-id"}},
    "/api/settings/backup-config": {"status": 200, "json": {}},
    "/api/policy/decision-pin": {"status": 200, "json": {"set": False}},
}


@pytest.fixture(scope="module")
def settings_script() -> str:
    from ha_mcp.settings_ui import _SETTINGS_HTML

    return extract_script_body(_SETTINGS_HTML)


def _element(el_id: str) -> str:
    if el_id == "modalBackdrop":
        return MODAL_SCAFFOLD
    if el_id in CHILD_OF_SCAFFOLD:
        return ""
    if el_id.startswith("backup") and el_id.endswith(("Domain", "Entity", "State")):
        return f'<select id="{el_id}"></select>'
    if el_id in TABLE_BODY_IDS:
        return f'<table><tbody id="{el_id}"></tbody></table>'
    if el_id in BUTTON_IDS or el_id.endswith("-btn"):
        return f'<button id="{el_id}"></button>'
    if el_id.endswith("-toggle"):
        return f'<input id="{el_id}" type="checkbox" />'
    if el_id == "search":
        return '<input id="search" />'
    if el_id == "restartNotice":
        return '<div id="restartNotice"><span id="restartNoticeText"></span></div>'
    return f'<div id="{el_id}"></div>'


@pytest.fixture(scope="module")
def min_dom(settings_script: str) -> str:
    """Every element the settings script looks up by a literal id."""
    ids = dict.fromkeys(re.findall(r"getElementById\('([^']+)'\)", settings_script))
    body = "\n".join(_element(i) for i in ids)
    return f"<!DOCTYPE html>\n<html><body>\n{body}\n</body></html>"


def _assert_clean_init(result: HarnessResult) -> None:
    init_errors = [
        e
        for e in result.errors
        if e.startswith(("script init:", "transpile failure", "invoke:", "jsdom error"))
    ]
    assert not init_errors, f"script failed to initialise: {init_errors}"


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
    settings_script: str, min_dom: str, redact_on: bool
) -> None:
    result = run_script(
        settings_script,
        initial_html=min_dom,
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


def test_turning_redact_secrets_on_unlocks_the_key_list(
    settings_script: str, min_dom: str
) -> None:
    result = run_script(
        settings_script,
        initial_html=min_dom,
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
