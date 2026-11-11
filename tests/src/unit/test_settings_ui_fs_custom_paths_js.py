"""Custom-paths save on the settings page when the answer is not JSON."""

from __future__ import annotations

import re

import pytest

from ._js_harness import extract_script_body, run_script

_SELECTS = ("backupDomain", "backupEntity", "backupState")
_BUTTONS = (
    "restartBtn",
    "stopSidecarBtn",
    "backupConfigSave",
    "backupRefresh",
    "backupBulkDelete",
    "policy-save-global-btn",
    "policy-set-pin-btn",
    "policy-clear-pin-btn",
)
_CHECKBOXES = (
    "policy-master-toggle",
    "policy-manage-tool-toggle",
    "read-only-mode-toggle",
)
_MODAL = (
    '<div id="modalBackdrop"><div class="modal" role="dialog" tabindex="-1">'
    '<span id="modalTitle"></span><button id="modalClose">x</button>'
    '<div id="modalBody"></div></div></div>'
)


def _stub(el_id: str) -> str:
    if el_id in _SELECTS:
        return f'<select id="{el_id}"></select>'
    if el_id in _BUTTONS:
        return f'<button id="{el_id}"></button>'
    if el_id in _CHECKBOXES:
        return f'<input id="{el_id}" type="checkbox" />'
    if el_id == "search":
        return '<input id="search" />'
    if el_id in ("backupList", "featuresBody"):
        return f'<table><tbody id="{el_id}"></tbody></table>'
    return f'<div id="{el_id}"></div>'


def _min_dom(script: str) -> str:
    """One stub per element the script binds a handler on at load time."""
    ids = dict.fromkeys(
        re.findall(r"^document\.getElementById\('([^']+)'\)", script, re.M)
    )
    # Children of the modal and the restart notice live inside their parents.
    nested = {"modalBody", "modalTitle", "modalClose", "restartNoticeText"}
    rows = []
    for el_id in ids:
        if el_id == "modalBackdrop":
            rows.append(_MODAL)
        elif el_id == "restartNotice":
            rows.append(
                '<div id="restartNotice"><span id="restartNoticeText"></span></div>'
            )
        elif el_id not in nested:
            rows.append(_stub(el_id))
    return "<!DOCTYPE html><html><body>" + "".join(rows) + "</body></html>"


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

_EDITOR = (
    '<textarea id="fsCustomPathsInput">pyscript</textarea>'
    '<button id="fsCustomPathsSave"></button>'
    '<div id="fsCustomPathsStatus"></div>'
)


@pytest.mark.parametrize("status", [502, 200])
def test_save_names_the_status_of_a_proxy_error_page(status: int) -> None:
    """A proxy replaces the body of a 502, so there is no JSON to parse."""
    from ha_mcp.settings_ui import _SETTINGS_HTML

    script = extract_script_body(_SETTINGS_HTML)
    result = run_script(
        script,
        initial_html=_min_dom(script).replace("</body>", _EDITOR + "</body>"),
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/fs-custom-paths": {
                "status": status,
                "body": "<html>Bad gateway</html>",
            },
        },
        invoke="await saveFsCustomPaths();",
    )

    assert f"Save failed (HTTP {status})" in result.dom
    assert "SyntaxError" not in result.dom
