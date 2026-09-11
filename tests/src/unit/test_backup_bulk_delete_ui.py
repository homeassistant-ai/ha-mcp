"""Bulk deletion reports retained snapshots through the shipped Settings script."""

import json

import pytest
from starlette.requests import Request

from ._js_harness import HarnessResult, extract_script_body, run_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES


def _delete_backups(
    reply: dict, *, locale: str = "en", messages=None, response=None
) -> HarnessResult:
    from ha_mcp.settings_ui import _render_settings_html

    page = _render_settings_html(
        Request(
            {
                "type": "http",
                "query_string": b"",
                "headers": [(b"accept-language", locale.encode())],
            }
        )
    )
    result = run_script(
        extract_script_body(page),
        initial_html=page,
        prelude=(
            "window.prompt = () => '';"
            "const catalogElement = document.getElementById('ha-mcp-i18n');"
            "const catalog = JSON.parse(catalogElement.textContent);"
            f"Object.assign(catalog.messages, {json.dumps(messages or {})});"
            "catalogElement.textContent = JSON.stringify(catalog);"
        ),
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/backups?": {
                "byMethod": {
                    "DELETE": response or {"status": 200, "json": reply},
                    "GET": {"status": 200, "json": {"backups": []}},
                }
            },
        },
        invoke=(
            "await new Promise(resolve => setTimeout(resolve, 100));"
            "const domain = document.getElementById('backupDomain');"
            "domain.value = 'helper_template';"
            "document.getElementById('backupBulkDelete').click();"
        ),
        settle_ms=300,
    )
    assert not result.errors
    requests = result.fetches_to("/api/settings/backups?")
    assert [request["method"] for request in requests] == ["DELETE", "GET"]
    assert "domain=helper_template" in requests[0]["url"]
    assert len(result.confirms) == 1
    return result


@pytest.mark.parametrize("locale", ["en", "fr"])
def test_partial_delete_reports_in_use_and_other_failures(
    locale: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incomplete catalogs inherit the new English result and retry guidance."""
    if locale != "en":
        from ha_mcp.settings_ui._i18n import CATALOGS

        for key in ("backup.bulk.partial", "backup.bulk.in_use"):
            monkeypatch.delitem(CATALOGS[locale]["messages"], key, raising=False)
    result = _delete_backups(
        {
            "success": True,
            "deleted": ["deleted.yaml"],
            "failed": ["active.yaml", "unavailable.yaml"],
            "count": 1,
            "failure_reasons": {"active.yaml": "snapshot_in_use"},
        },
        locale=locale,
    )
    assert result.alerts == [
        "Deleted 1 backup(s); failed to delete 2 backup(s).\n\n"
        "1 backup(s) are in use. Retry after the active capture or restore finishes."
    ]


@pytest.mark.parametrize("reason", [None, "private-error-detail"])
def test_generic_delete_failure_is_visible_without_invented_reason(reason) -> None:
    reply = {"success": True, "deleted": [], "failed": ["failed.yaml"], "count": 0}
    if reason is not None:
        reply["failure_reasons"] = {"failed.yaml": reason}
    result = _delete_backups(reply)
    assert result.alerts == ["Deleted 0 backup(s); failed to delete 1 backup(s)."]


def test_partial_delete_uses_translated_summary_and_retry_guidance() -> None:
    result = _delete_backups(
        {
            "success": True,
            "deleted": ["deleted.yaml"],
            "failed": ["active.yaml"],
            "count": 1,
            "failure_reasons": {"active.yaml": "snapshot_in_use"},
        },
        messages={
            "backup.bulk.partial": "removed={deleted}; retained={failed}",
            "backup.bulk.in_use": "busy={count}; retry when idle",
        },
    )
    assert result.alerts == ["removed=1; retained=1\n\nbusy=1; retry when idle"]


def test_all_in_use_reports_zero_deletions_and_retry_guidance() -> None:
    result = _delete_backups(
        {
            "success": True,
            "deleted": [],
            "failed": ["capture.yaml", "restore.yaml"],
            "count": 0,
            "failure_reasons": {
                "capture.yaml": "snapshot_in_use",
                "restore.yaml": "snapshot_in_use",
            },
        }
    )
    assert result.alerts == [
        "Deleted 0 backup(s); failed to delete 2 backup(s).\n\n"
        "2 backup(s) are in use. Retry after the active capture or restore finishes."
    ]


def test_successful_bulk_delete_keeps_existing_result() -> None:
    result = _delete_backups(
        {"success": True, "deleted": ["deleted.yaml"], "failed": [], "count": 1}
    )
    assert result.alerts == ["Deleted 1 backup(s)"]


@pytest.mark.parametrize(
    "response",
    [
        {"throw": "connection lost after DELETE"},
        {"status": 500, "body": "<html>private-error-body</html>"},
        {"status": 200, "body": "{truncated"},
    ],
)
def test_bulk_delete_response_failure_shows_toast_and_refreshes_inventory(response):
    result = _delete_backups({}, response=response)

    assert not result.alerts
    assert "Bulk delete failed" in result.dom
    assert "could not be confirmed" in result.dom
    assert "before retrying" in result.dom
    assert "private-error-body" not in result.dom


@pytest.mark.parametrize("translated", [False, True])
def test_single_delete_in_use_renders_retry_guidance(translated):
    from ha_mcp.settings_ui import _render_settings_html

    name = "automation.example.20260909_000000.yaml"
    page = _render_settings_html(
        Request({"type": "http", "query_string": b"", "headers": []})
    )
    messages = {"backup.delete.in_use": "busy; retry when idle"} if translated else {}
    result = run_script(
        extract_script_body(page),
        initial_html=page,
        prelude=(
            "const catalogElement = document.getElementById('ha-mcp-i18n');"
            "const catalog = JSON.parse(catalogElement.textContent);"
            f"Object.assign(catalog.messages, {json.dumps(messages)});"
            "catalogElement.textContent = JSON.stringify(catalog);"
        ),
        fetch_map={
            **DEFAULT_FETCHES,
            f"/api/settings/backups/{name}": {
                "status": 409,
                "json": {
                    "success": False,
                    "error": {
                        "code": "SERVICE_CALL_FAILED",
                        "message": "Snapshot in use",
                    },
                    "data": {"reason": "snapshot_in_use"},
                },
            },
        },
        invoke=(
            "await new Promise(resolve => setTimeout(resolve, 100));"
            f"await window.backupAction('delete', {json.dumps(name)});"
        ),
        settle_ms=300,
    )

    assert not result.errors
    expected = (
        "busy; retry when idle"
        if translated
        else "This backup is in use. Retry after the active capture or restore finishes."
    )
    assert result.alerts == [f"Delete failed: {expected}"]
    assert len(result.fetches_to(f"/api/settings/backups/{name}")) == 1
