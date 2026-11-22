"""The Home Assistant sidebar panel's settings export and import controls."""

from __future__ import annotations

import importlib.resources
import json
import re

import pytest

from custom_components.ha_mcp_tools.settings_transfer_schema import RefusalCode
from ha_mcp.settings_ui import render_settings_html

from ._js_harness import HarnessResult, extract_script_body, run_script
from ._settings_ui_dom import DEFAULT_FETCHES

_PANEL = "/api/ha_mcp_tools/ui/app/settings"
_TRANSFER = "./api/settings/transfer/"
_BUNDLE = '{"format":"ha-mcp-settings","version":1,"entries":{"server":{"webhook_id":"test-private-id"}}}'
_EN = json.loads(
    (importlib.resources.files("ha_mcp.settings_ui") / "locales" / "en.json").read_text(
        encoding="utf-8"
    )
)["messages"]


def _run(
    invoke: str,
    *,
    path: str = _PANEL,
    responses: dict | None = None,
    prelude: str = "",
) -> HarnessResult:
    page = render_settings_html()
    result = run_script(
        extract_script_body(page),
        initial_html=page,
        prelude=f"history.replaceState(null, '', {json.dumps(path)});" + prelude,
        fetch_map={**DEFAULT_FETCHES, **(responses or {})},
        invoke=invoke
        + """
            await new Promise(resolve => setTimeout(resolve, 0));
            const region = document.getElementById('transferStatus');
            __captureStatus(region.getAttribute('role') + ': ' + region.textContent);
        """,
    )
    assert not result.errors
    return result


def _choose(contents: str, size: int = 100) -> str:
    # JSDOM does not implement the native file picker or File.text().
    return f"""
        const input = document.getElementById('transferFile');
        Object.defineProperty(input, 'files', {{
          get: () => [{{size: {size}, text: async () => {json.dumps(contents)}}}],
        }});
        input.dispatchEvent(new Event('change'));
        document.getElementById('transferImport').click();
    """


def test_a_direct_server_page_imports_through_its_own_routes() -> None:
    """Standalone, Docker and the app serve the routes the sidebar proxy does."""
    result = _run(
        _choose(_BUNDLE),
        path="/mcp/settings",
        responses={
            _TRANSFER + "import": {
                "json": {
                    "success": True,
                    "restart_required": True,
                    "restart": "server",
                    "skipped": ["connection", "file_permissions"],
                }
            }
        },
    )
    assert not re.search(r'<section id="settingsTransfer"[^>]* hidden', result.dom)
    assert [r["method"] for r in result.fetches_to(_TRANSFER + "import")] == ["POST"]
    assert result.status == "status: " + " ".join(
        _EN[f"transfer.{key}"]
        for key in ("restart_server", "connection_skipped", "file_permissions_skipped")
    )


def test_download_preserves_the_export_without_rendering_credentials() -> None:
    result = _run(
        "document.getElementById('transferExport').click();",
        responses={_TRANSFER + "export": {"body": _BUNDLE}},
        prelude="""
            // Observe the browser download instead of navigating in JSDOM.
            URL.createObjectURL = blob => {
              document.body.dataset.blobSize = String(blob.size);
              document.body.dataset.mime = blob.type;
              return 'blob:https://test.local/test-download';
            };
            URL.revokeObjectURL = () => { document.body.dataset.revoked = 'yes'; };
            HTMLAnchorElement.prototype.click = function () {
              document.body.dataset.filename = this.download;
            };
        """,
    )
    assert not re.search(r'<section id="settingsTransfer"[^>]* hidden', result.dom)
    assert [r["method"] for r in result.fetches_to(_TRANSFER + "export")] == ["POST"]
    assert f'data-blob-size="{len(_BUNDLE.encode())}"' in result.dom
    assert 'data-mime="application/json"' in result.dom
    assert re.search(
        r'data-filename="ha-mcp-settings-\d{4}-\d{2}-\d{2}\.json"', result.dom
    )
    assert 'data-revoked="yes"' in result.dom
    assert "test-private-id" not in result.dom
    assert result.status == "status: Settings download started."


def test_an_export_without_permissions_says_so() -> None:
    """OAuth mode's settings page cannot read File & YAML Tools permissions."""
    result = _run(
        "document.getElementById('transferExport').click();",
        path="/mcp/settings",
        responses={
            _TRANSFER + "export": {
                "body": _BUNDLE,
                "headers": {"X-HA-MCP-Skipped": "file_permissions_unreachable"},
            }
        },
        prelude="URL.createObjectURL = () => 'blob:https://test.local/x';"
        "URL.revokeObjectURL = () => {};"
        "HTMLAnchorElement.prototype.click = () => {};",
    )
    assert result.status == "status: " + " ".join(
        _EN[f"transfer.{key}"]
        for key in ("downloaded", "export_file_permissions_unreachable")
    )


def test_an_import_without_reachable_permissions_says_so() -> None:
    result = _run(
        _choose(_BUNDLE),
        path="/mcp/settings",
        responses={
            _TRANSFER + "import": {
                "json": {"success": True, "skipped": ["file_permissions_unreachable"]}
            }
        },
    )
    assert result.status == "status: " + " ".join(
        _EN[f"transfer.{key}"]
        for key in ("imported", "file_permissions_unreachable_skipped")
    )


def test_unusable_source_entries_point_to_the_integration_configuration() -> None:
    result = _run(
        "document.getElementById('transferExport').click();",
        responses={
            _TRANSFER + "export": {"status": 400, "json": {"error": "invalid_backup"}}
        },
    )
    assert result.status and "Open Configure" in result.status
    assert "download started" not in result.status


def test_cancel_import_keeps_the_file_local() -> None:
    result = _run(_choose(_BUNDLE), prelude="window.confirm = () => false;")
    assert not result.fetches_to(_TRANSFER)


@pytest.mark.parametrize(
    ("operation", "code", "setting"),
    [
        ("export", "allowed_paths_store_unavailable", "allowed directories"),
        ("import", "extra_yaml_keys_store_unavailable", "extra YAML keys"),
    ],
)
def test_storage_error_identifies_the_affected_setting(
    operation: str, code: str, setting: str
) -> None:
    action = (
        _choose(_BUNDLE)
        if operation == "import"
        else "document.getElementById('transferExport').click();"
    )
    result = _run(
        action,
        responses={_TRANSFER + operation: {"status": 503, "json": {"error": code}}},
    )
    assert result.status and result.status.startswith("alert:")
    assert setting in result.status


@pytest.mark.parametrize(
    ("credentials", "prompt"),
    [(True, "transfer.confirm_connection"), (False, "transfer.confirm")],
)
def test_the_confirmation_names_connection_secrets_only_when_the_file_has_them(
    credentials: bool, prompt: str
) -> None:
    bundle = json.dumps(
        {"format": "ha-mcp-settings", "contains_credentials": credentials}
    )
    result = _run(
        _choose(bundle),
        responses={_TRANSFER + "import": {"json": {"success": True}}},
    )
    assert result.confirms == [_EN[prompt]]


@pytest.mark.parametrize("restart", [True, False])
def test_confirmed_import_sends_original_file_and_reports_next_step(
    restart: bool,
) -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {
                "json": {"success": True, "restart_required": restart}
            }
        },
    )
    assert len(result.confirms) == 1
    imports = result.fetches_to(_TRANSFER + "import")
    assert [(r["method"], r["body"]) for r in imports] == [("POST", _BUNDLE)]
    assert "test-private-id" not in result.dom
    assert result.status and result.status.startswith("status: Settings imported.")
    assert (
        "Restart Home Assistant" if restart else "Reload this page"
    ) in result.status


@pytest.mark.parametrize(
    ("contents", "size", "message"),
    [
        ("{broken", 10, "Choose a valid HA-MCP"),
        ('{"format":"other"}', 18, "Choose a valid HA-MCP"),
        (_BUNDLE, 4 * 1024 * 1024 + 1, "4 MiB or less"),
    ],
)
def test_invalid_file_never_prompts_or_uploads(
    contents: str, size: int, message: str
) -> None:
    result = _run(_choose(contents, size))
    assert not result.confirms
    assert not result.fetches_to(_TRANSFER)
    assert (
        result.status
        and result.status.startswith("alert:")
        and message in result.status
    )


def test_failed_import_never_renders_arbitrary_error_text() -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {"status": 400, "json": {"error": "test-private-id"}}
        },
    )
    assert "test-private-id" not in result.dom
    assert result.status and result.status.startswith("alert: HA-MCP could not finish")


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (500, "rollback_incomplete", "some settings could not be restored"),
        (403, "origin_denied", "Open the panel from the sidebar"),
        (409, "permissions_changed", "Import the file again"),
        (409, "component_outdated", "Update HA-MCP in Marketplace"),
    ],
)
def test_import_errors_explain_next_action(
    status: int, code: str, message: str
) -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={_TRANSFER + "import": {"status": status, "json": {"error": code}}},
    )
    assert result.status and result.status.startswith("alert:")
    assert message in result.status


@pytest.mark.parametrize("code", list(RefusalCode))
def test_every_refusal_shows_its_own_message(code: RefusalCode) -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {
                "status": code.http_status,
                "json": {"success": False, "error": code},
            }
        },
    )
    assert result.status == f"alert: {_EN[f'transfer.{code}']}"
