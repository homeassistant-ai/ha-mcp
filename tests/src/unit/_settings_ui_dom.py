"""The settings page's minimal DOM and default fetch replies for the JS behaviour tests."""

from __future__ import annotations

# Element ids the script binds top-level handlers on. Drift between
# this set and what the production script reaches via
# ``document.getElementById(...).addEventListener(...)`` is checked at
# import time of ``test_settings_ui_js_behavior.py`` by its
# ``_assert_min_dom_covers_handlers``, so a future
# handler that lands without a matching DOM stub fails the suite
# immediately with a clear message instead of letting tests silently
# fail at init.
_TOP_LEVEL_ELEMENT_IDS = [
    "status",
    "restartBtn",
    "restartNotice",
    "restartNoticeText",
    "sidecarStopRow",
    "stopSidecarBtn",
    "search",
    "groups",
    "summary",
    "featuresBody",
    "backupConfigForm",
    "backupConfigActions",
    "backupConfigSave",
    "backupConfigStatus",
    "backupRefresh",
    "backupBulkDelete",
    "backupDomain",
    "backupEntity",
    "backupState",
    "backupList",
    "modalBackdrop",
    "modalBody",
    "modalTitle",
    "modalClose",
    "panel-tools",
    "panel-server",
    "panel-backups",
    # Tool Security Policies tab (#966): the master-toggle checkbox
    # mirrors the Server-Settings flag and posts to /api/settings/features;
    # the global-settings save button writes wait_seconds / TTL.
    "policy-master-toggle",
    # enable_security_policy_tool (#2148) — registers
    # ha_manage_security_policy; same save-then-verify flow as the master.
    "policy-manage-tool-toggle",
    "policy-save-global-btn",
    "policy-set-pin-btn",
    "policy-clear-pin-btn",
    # Read Only Mode toggle (#1569) — Tools tab, above the search box.
    # Same save-then-verify flow as the policy master toggle.
    "read-only-mode-toggle",
    # Where applyFlagToggle writes the env-locked note for that switch.
    "read-only-locked",
    # Advanced settings panel — the 5 section containers that
    # loadAdvancedSettings() writes to via innerHTML. Without container
    # divs in MIN_DOM, renderSection silently no-ops (getElementById
    # returns null) and the behavioural tests find an empty body.
    # (The advanced fields auto-save on change, so there is no Save
    # button/status row here anymore.)
    # Connection section was removed from the panel;
    # advSearch is now the first rendered advanced section.
    "advSearch",
    "advOperations",
    "advToolsSurface",
    "advDiagnostics",
    # Developer section (issue #1775) — bottom of panel-server; hosts the
    # dev-mode toggle whose enable path is confirm()-gated.
    "advDeveloper",
    # Beta features dedicated container — beta
    # master + sub-flags render here, NOT into featuresBody, so the
    # dangerous block sits at the bottom of panel-server.
    "betaBody",
    # Version footer span — populated from /api/settings/info on
    # init; without the container element the JS would no-op.
    "versionFooterText",
]


def _min_dom_row(el_id: str) -> str | None:
    """DOM stub markup for ``el_id`` (first half of the dispatch); None emits nothing."""
    if el_id.startswith("backup") and el_id.endswith(("Domain", "Entity", "State")):
        return f'<select id="{el_id}"></select>'
    if el_id in ("backupList", "featuresBody"):
        return f'<table><tbody id="{el_id}"></tbody></table>'
    if el_id == "modalBackdrop":
        # Full modal scaffold (mirrors settings.html) so the focus-trap
        # code in showModal/closeModal — which queries `.modal` and
        # #modalClose — has the structure it depends on.
        return (
            '<div id="modalBackdrop">'
            '<div class="modal" role="dialog" tabindex="-1">'
            '<div class="modal-header">'
            '<span class="modal-title" id="modalTitle"></span>'
            '<button class="modal-close" id="modalClose">×</button>'
            "</div>"
            '<div class="modal-body" id="modalBody"></div>'
            "</div>"
            "</div>"
        )
    if el_id in ("modalBody", "modalTitle", "modalClose"):
        return None  # rendered as children of modalBackdrop above
    if el_id.startswith("panel-"):
        return f'<div id="{el_id}" class="panel"></div>'
    return _min_dom_row_tail(el_id)


def _min_dom_row_tail(el_id: str) -> str | None:
    """DOM stub markup for ``el_id`` (second half of the dispatch); None emits nothing."""
    if el_id in (
        "restartBtn",
        "stopSidecarBtn",
        "backupConfigSave",
        "backupRefresh",
        "backupBulkDelete",
    ):
        return f'<button id="{el_id}"></button>'
    if el_id == "restartNotice":
        return '<div id="restartNotice"><span id="restartNoticeText"></span></div>'
    if el_id == "restartNoticeText":
        return None  # rendered as a child of restartNotice above
    if el_id == "search":
        return '<input id="search" />'
    if el_id in (
        "policy-master-toggle",
        "policy-manage-tool-toggle",
        "read-only-mode-toggle",
    ):
        return f'<input id="{el_id}" type="checkbox" />'
    if el_id in (
        "policy-save-global-btn",
        "policy-set-pin-btn",
        "policy-clear-pin-btn",
    ):
        return f'<button id="{el_id}"></button>'
    return f'<div id="{el_id}"></div>'


def _build_min_dom() -> str:
    """Render an HTML document that supplies every element the script
    binds a top-level handler on, so the init pass doesn't throw on a
    missing-element addEventListener call.
    """
    rows = []
    for el_id in _TOP_LEVEL_ELEMENT_IDS:
        row = _min_dom_row(el_id)
        if row is not None:
            rows.append(row)
    body = "\n  ".join(rows)
    return f"<!DOCTYPE html>\n<html><body>\n  {body}\n</body></html>"


MIN_DOM = _build_min_dom()


# Default routes for the init-time fetches. Individual tests merge in
# their own entries via ``{**DEFAULT_FETCHES, "/restart": ...}``.
DEFAULT_FETCHES: dict[str, dict] = {
    "/api/settings/features": {
        "status": 200,
        "json": {"features": [], "values": {}},
    },
    "/api/settings/tools": {
        "status": 200,
        "json": {"tools": [], "states": {}, "addon_mode": True},
    },
    "/api/settings/info": {
        "status": 200,
        "json": {"instance_id": "baseline-id"},
    },
    "/api/settings/backup-config": {
        "status": 200,
        "json": {},
    },
    # policyLoadConfig asks whether an approval PIN exists on every load; the
    # PIN lives outside the policy document, so it is its own endpoint.
    "/api/policy/decision-pin": {
        "status": 200,
        "json": {"set": False},
    },
}
